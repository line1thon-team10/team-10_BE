from sqlalchemy import func, select
import pytest

from app.ai.gateway import FunctionRef, ProviderSpec
from app.db import schema as t
from tests.test_material_storage import material_case, pdf_bytes, upload


@pytest.fixture
def tutor_case(api_client, material_case, monkeypatch):
    from app.services import tutor
    monkeypatch.setenv('GROQ_API_KEY', 'fixture-only-not-a-real-key')
    monkeypatch.setattr(tutor, 'PROVIDER', ProviderSpec(FunctionRef('tests.e2e.tutor_provider_transport', 'provider')))
    raw = pdf_bytes('AVL rotations preserve the search tree ordering. ' * 3)
    response = upload(api_client, material_case, raw)
    assert response.status_code == 200, response.text
    return {**material_case, **response.json()['data']}


def ask(client, case, question='Explain AVL rotations', history=None, user=None):
    body = {'question': question}
    if history is not None:
        body['history'] = history
    return client.post('/api/tutor', headers={'X-Anonymous-Id': user or case['user']}, json=body)


def history_of(response):
    value = response.json()['data']
    return [{key: value[key] for key in ('question', 'answer', 'pages')}]


def test_actual_uploaded_pdf_answer_is_persisted_and_reused_without_second_transport(
        api_client, tutor_case, domain_connection):
    case, conn = tutor_case, domain_connection
    first = ask(api_client, case)
    assert first.status_code == 200, first.text
    value = first.json()['data']
    assert value['subjectId'] == case['subject'] and value['parentId'] is None and value['saved'] is False
    assert value['pages'] == [{'materialId': case['materialId'], 'from': 1, 'to': 1}]
    source = conn.execute(select(t.tutor_sources)).mappings().one()
    assert 'AVL rotations' in source['quote']
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == 1
    conn.commit()
    second = ask(api_client, case)
    assert second.status_code == 200, second.text
    assert second.json()['data'] == {**value, 'saved': True}
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 1
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == 1


def test_easy_explanation_is_bound_to_one_owned_stored_original(api_client, tutor_case, domain_connection):
    case, conn = tutor_case, domain_connection
    first = ask(api_client, case)
    assert first.status_code == 200, first.text
    history = history_of(first)
    easy = ask(api_client, case, 'Explain more simply', history)
    assert easy.status_code == 200, easy.text
    value = easy.json()['data']
    assert value['parentId'] == first.json()['data']['id'] and value['pages'] == first.json()['data']['pages']
    assert value['question'] == first.json()['data']['question']
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 2
    conn.commit()
    again = ask(api_client, case, 'Even simpler wording', history)
    assert again.status_code == 200 and again.json()['data']['id'] == value['id'] and again.json()['data']['saved']
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 2
    conn.commit()
    rejected = ask(api_client, case, 'Simplify', history_of(easy))
    assert rejected.status_code == 400


@pytest.mark.parametrize('mutation', ['two', 'answer', 'question', 'page', 'foreign'])
def test_forged_or_multiturn_history_never_reaches_provider(api_client, tutor_case, domain_connection, mutation):
    case, conn = tutor_case, domain_connection
    first = ask(api_client, case)
    assert first.status_code == 200, first.text
    history = history_of(first)
    if mutation == 'two': history *= 2
    if mutation == 'answer': history[0]['answer'] = 'Forged answer'
    if mutation == 'question': history[0]['question'] = 'Different question'
    if mutation == 'page': history[0]['pages'][0]['to'] = 99
    before = conn.scalar(select(func.count()).select_from(t.ai_usage_logs))
    conn.commit()
    response = ask(api_client, case, 'Easy', history, user=case['other_user'] if mutation == 'foreign' else None)
    assert response.status_code == (422 if mutation == 'two' else 400), response.text
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == before


@pytest.mark.parametrize('mode', ['no_cites', 'outside', 'boolean', 'duplicate', 'extra', 'empty', 'json', 'fail'])
def test_invalid_or_failed_provider_never_saves_a_fake_success(api_client, tutor_case, domain_connection, monkeypatch, mode):
    case, conn = tutor_case, domain_connection
    monkeypatch.setenv('TUTOR_TEST_MODE', mode)
    response = ask(api_client, case)
    assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 0
    assert conn.scalar(select(func.count()).select_from(t.tutor_sources)) == 0
    job = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.kind == 'tutor')).mappings().one()
    assert job['status'] == 'failed' and job['input_payload']['payload']['question'] == 'Explain AVL rotations'


def test_no_evidence_and_other_owner_files_cannot_supply_an_answer(api_client, tutor_case, domain_connection):
    case, conn = tutor_case, domain_connection
    result = ask(api_client, case, user=case['other_user'])
    assert result.status_code == 503
    assert conn.scalar(select(func.count()).select_from(t.tutor_messages)) == 0
    assert conn.scalar(select(func.count()).select_from(t.ai_usage_logs)) == 0
