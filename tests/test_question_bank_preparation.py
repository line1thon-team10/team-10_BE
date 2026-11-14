from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.ai.gateway import AiGateway, FunctionRef, ProviderSpec
from app.core.responses import ApiError
from app.db import schema as t
from app.services.material_analysis import MaterialAnalysisService
from app.services.question_bank import QuestionBankService
from tests.test_material_storage import material_case, pdf_bytes, upload


class TransportGateway(AiGateway):
    def run(self, request, *, validator, provider=None, **kwargs):
        module = ('tests.e2e.question_bank_transport' if request.identity.kind == 'question_pregenerate'
                  else 'tests.e2e.material_provider_transport')
        return super().run(request, validator=validator,
            provider=ProviderSpec(FunctionRef(module, 'provider_with_test_transport')), **kwargs)


@pytest.fixture
def prepared_material(api_client, material_case, domain_connection):
    from app.db.session import engine
    case = material_case
    raw = pdf_bytes('The uploaded document explains important concepts. ' * 8)
    response = upload(api_client, case, raw)
    assert response.status_code == 200, response.text
    ids = response.json()['data']
    worker = MaterialAnalysisService(engine, storage_dir=case['root'], gateway=TransportGateway(engine))
    assert worker.run_next()
    state = api_client.get(f"/api/materials/jobs/{ids['jobId']}", headers=case['headers'])
    assert state.json()['data']['status'] == 'done', state.text
    assert domain_connection.scalar(select(func.count()).select_from(t.questions)) == 0
    domain_connection.commit()
    bank = QuestionBankService(engine, storage_dir=case['root'], gateway=TransportGateway(engine))
    return case, ids, worker, bank


def bank_jobs(conn):
    return list(conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.kind == 'question_pregenerate')
        .order_by(t.ai_jobs.c.created_at, t.ai_jobs.c.id)).mappings())


def test_material_worker_prepares_grounded_banks_once_for_all_difficulties(api_client,domain_connection,prepared_material):
    case, ids, worker, bank = prepared_material; conn = domain_connection
    for _ in range(3): assert worker.run_next()
    assert not worker.run_next()
    questions = list(conn.execute(select(t.questions)).mappings())
    assert len(questions) == 30
    assert {q['difficulty'] for q in questions} == {'low', 'mid', 'high'}
    assert {q['type'] for q in questions} == {'choice', 'short', 'essay'}
    assert all(q['concept_id'] and q['correct_answer'] and q['explanation'] for q in questions)
    assert all(job['status'] == 'succeeded' and job['saved_result_id'] for job in bank_jobs(conn))
    sources = list(conn.execute(select(t.question_sources)).mappings())
    assert len(sources) == 30 and {s['material_id'] for s in sources} == {ids['materialId']}
    assert all(s['start_page'] == s['end_page'] == 1 and s['quote'] for s in sources)
    conn.commit()
    before = len(questions)
    for _ in range(2):
        response = api_client.get(f"/api/quiz/{case['subject']}", headers=case['headers'])
        assert response.status_code == 404
    assert conn.scalar(select(func.count()).select_from(t.questions)) == before


@pytest.mark.parametrize('mode', ['failed', 'invalid'])
def test_bank_failure_does_not_turn_analysis_into_failure_and_retry_is_durable(
        api_client,domain_connection,prepared_material,monkeypatch,mode):
    case, ids, worker, bank = prepared_material; conn = domain_connection
    monkeypatch.setenv('BANK_E2E_TRANSPORT', mode)
    assert bank.run_next()
    job = bank_jobs(conn)[0]
    assert job['status'] == 'failed' and job['error_code'] == 'AI_FAILED'
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 0
    conn.commit()
    assert api_client.get(f"/api/materials/jobs/{ids['jobId']}", headers=case['headers']).json()['data']['status'] == 'done'
    assert not bank.run_job(case['user'], job['id'])
    with pytest.raises(ApiError) as error: bank.retry(case['other_user'], job['id'])
    assert error.value.status == 404
    monkeypatch.setenv('BANK_E2E_TRANSPORT', 'valid')
    assert bank.retry(case['user'], job['id'])
    assert bank.run_next()
    assert bank_jobs(conn)[0]['status'] == 'succeeded'
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 10


def test_bank_rechecks_structure_before_publish_and_preserves_analysis(domain_connection,prepared_material):
    case, ids, worker, bank = prepared_material; conn = domain_connection
    user, identifier = bank._discover(lambda: False)
    claim = bank._claim(user, identifier)
    request, context = bank._prepare(claim)
    outcome = bank.gateway.run(request, validator=bank.validator, provider=bank.provider)
    conn.execute(update(t.concepts).values(title='Edited concept'))
    conn.commit()
    with pytest.raises(ApiError) as error: bank._finish(claim, outcome, context)
    assert error.value.code == 'QUESTION_BANK_STALE'
    bank._fail(claim, error.value)
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 0
    assert bank_jobs(conn)[0]['error_code'] == 'QUESTION_BANK_STALE'


def test_live_claim_cannot_duplicate_and_expired_claim_cannot_publish(domain_connection,prepared_material):
    case, ids, worker, bank = prepared_material; conn = domain_connection
    user, identifier = bank._discover(lambda: False)
    now = bank.clock(); bank.clock = lambda: now
    claim = bank._claim(user, identifier)
    request, context = bank._prepare(claim)
    assert not bank.run_job(user, identifier)
    now += timedelta(seconds=bank.settings.pdf_job_budget_seconds + 11)
    assert bank.run_job(user, identifier)
    outcome = bank.gateway.run(request, validator=bank.validator, provider=bank.provider)
    assert not bank._finish(claim, outcome, context)
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 10
    assert bank_jobs(conn)[0]['retry_count'] == 2


@pytest.mark.parametrize('when', ['before_admission', 'after_claim', 'after_provider'])
def test_archived_subject_never_gets_a_new_bank(domain_connection,prepared_material,when):
    case, ids, worker, bank = prepared_material; conn = domain_connection
    if when != 'before_admission':
        user, identifier = bank._discover(lambda: False)
        claim = bank._claim(user, identifier)
        if when == 'after_provider':
            request, context = bank._prepare(claim)
            outcome = bank.gateway.run(request, validator=bank.validator, provider=bank.provider)
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject']).values(status='archived'))
    conn.commit()
    if when == 'before_admission':
        assert not bank.run_next()
        assert bank_jobs(conn) == []
    else:
        with pytest.raises(ApiError) as error:
            if when == 'after_claim': bank._prepare(claim)
            else: bank._finish(claim, outcome, context)
        assert error.value.code == 'QUESTION_BANK_STALE'
        bank._fail(claim, error.value)
        assert bank_jobs(conn)[0]['status'] == 'failed'
        conn.commit()
        with pytest.raises(ApiError): bank.retry(user, identifier)
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 0
