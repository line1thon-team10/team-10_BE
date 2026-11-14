"""Independent HTTP/DB contracts for durable material analysis, without a browser.

Only the external provider is substituted. Real upload parsing, storage, worker
claims, gateway cache/telemetry, validation and DB finalization stay in the flow.
"""
from datetime import timedelta
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import select, update

from app.core.clock import now_utc
from app.db import schema as t
from tests.test_material_storage import material_case, upload


JOB_KEYS = {'jobId', 'materialId', 'subjectId', 'status', 'ocr', 'steps',
            'current', 'failedStep', 'progress', 'reused', 'reason'}


def two_page_pdf():
    with pymupdf.open() as document:
        for number in range(2):
            page = document.new_page()
            page.insert_text((20, 30), f'Page {number + 1}: ' + 'Learning material text ' * 8)
        return document.tobytes()


def accepted(client, case):
    raw = two_page_pdf()
    response = upload(client, case, raw)
    assert response.status_code == 200, response.text
    return response.json()['data'], raw


def job_view(client, case, job_id):
    response = client.get(f'/api/materials/jobs/{job_id}', headers=case['headers'])
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    data = response.json()['data']
    assert set(data) == JOB_KEYS
    assert data['jobId'] == job_id
    assert data['subjectId'] == case['subject']
    assert type(data['steps']) is list and data['steps']
    assert all(type(label) is str and label for label in data['steps'])
    assert type(data['current']) is int and 0 <= data['current'] < len(data['steps'])
    assert type(data['progress']) is int
    assert type(data['reused']) is bool
    assert str(case['root']) not in response.text
    assert 'storageKey' not in response.text and 'sourceSha256' not in response.text
    if data['status'] == 'done':
        assert data['progress'] == 100
        assert type(data['ocr']) is bool
        assert data['failedStep'] is None
    elif data['status'] == 'running':
        assert 0 <= data['progress'] <= 99
        assert data['failedStep'] is None
    else:
        assert data['status'] == 'failed'
        assert data['failedStep'] == data['current']
        assert type(data['reason']) is str and data['reason']
    return data


def test_http_upload_exposes_owned_durable_queued_job_and_original(
        api_client, material_case, domain_connection):
    case, conn = material_case, domain_connection
    ids, raw = accepted(api_client, case)
    view = job_view(api_client, case, ids['jobId'])
    assert view['materialId'] == ids['materialId']
    assert view['status'] == 'running' and view['progress'] == 0
    assert view['ocr'] is None and view['reason'] is None
    job = conn.execute(select(t.ai_jobs)).mappings().one()
    assert job['status'] == 'queued' and job['target_id'] == ids['materialId']
    assert job['user_id'] == case['user']
    assert job['started_at'] is None and job['finished_at'] is None
    conn.commit()
    response = api_client.get(f"/api/materials/{ids['materialId']}/file", headers=case['headers'])
    assert response.status_code == 200 and response.content == raw
    for unknown in (ids['jobId'], str(uuid4())):
        other = api_client.get(f'/api/materials/jobs/{unknown}',
            headers={'X-Anonymous-Id': case['other_user']})
        assert other.status_code == 404, other.text
    other = api_client.get(f"/api/materials/jobs/{uuid4()}", headers=case['headers'])
    assert other.status_code == 404
    assert api_client.get('/api/materials/jobs/not-a-uuid', headers=case['headers']).status_code == 422
    again = upload(api_client, case, raw)
    assert again.status_code == 200 and again.json()['data'] == ids
    assert conn.execute(select(t.ai_jobs)).mappings().one()['id'] == ids['jobId']


def blank_pdf():
    with pymupdf.open() as document:
        document.new_page()
        document.new_page()
        return document.tobytes()


@pytest.fixture
def worker(material_case, monkeypatch):
    from app.ai.gateway import AiGateway, FunctionRef, ProviderSpec
    from app.ai.settings import AiSettings
    from app.db.session import engine
    from app.services.material_analysis import MaterialAnalysisService

    class TransportGateway(AiGateway):
        def run(self, request, *, validator, provider=None, **kwargs):
            return super().run(request, validator=validator, provider=ProviderSpec(FunctionRef(
                'tests.e2e.material_provider_transport', 'provider_with_test_transport')), **kwargs)

    gateway = TransportGateway(engine, settings=AiSettings(pdf_job_budget_seconds=20))
    return MaterialAnalysisService(engine, storage_dir=material_case['root'], gateway=gateway)


def listed_file(client, case, material_id):
    response = client.get('/api/materials', headers=case['headers'])
    assert response.status_code == 200
    return next(file for subject in response.json()['data']['subjects']
                for file in subject['files'] if file['id'] == material_id)


@pytest.mark.parametrize('mode,ocr,quizless,calls', [
    ('native', False, False, 3), ('ocr_good', True, False, 4), ('ocr_low_quality', True, True, 1)])
def test_upload_worker_result_download_and_quality_are_one_persisted_flow(
        api_client, material_case, domain_connection, worker, monkeypatch, mode, ocr, quizless, calls):
    case, conn = material_case, domain_connection
    monkeypatch.setenv('MATERIAL_E2E_TRANSPORT', mode)
    raw = blank_pdf() if ocr else two_page_pdf()
    uploaded = upload(api_client, case, raw)
    assert uploaded.status_code == 200, uploaded.text
    ids = uploaded.json()['data']
    assert worker.run_job(case['user'], ids['jobId'])
    view = job_view(api_client, case, ids['jobId'])
    assert view['status'] == 'done' and view['ocr'] is ocr
    listing = listed_file(api_client, case, ids['materialId'])
    assert listing['status'] == 'done' and listing['ocr'] is ocr and listing['quizless'] is quizless
    units = conn.execute(select(t.units).where(t.units.c.material_id == ids['materialId'])).mappings().all()
    assert len(units) == (0 if quizless else 1)
    if units:
        assert units[0]['start_page'] == 1 and units[0]['end_page'] == 2
        assert units[0]['title'] == 'Core ideas'
        concepts = conn.execute(select(t.concepts).where(t.concepts.c.unit_id == units[0]['id'])).mappings().all()
        assert len(concepts) == 1 and concepts[0]['title'] == 'Important concept'
    usage = conn.execute(select(t.ai_usage_logs).where(t.ai_usage_logs.c.job_id == ids['jobId'])).mappings().all()
    assert len(usage) == calls and all(row['status'] == 'succeeded' for row in usage)
    assert all(row['user_id'] == case['user'] for row in usage)
    snapshot = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all(),
                conn.execute(select(t.ai_jobs)).all(), conn.execute(select(t.ai_usage_logs)).all())
    conn.commit()
    assert not worker.run_job(case['user'], ids['jobId'])
    assert upload(api_client, case, raw).json()['data'] == ids
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all(),
            conn.execute(select(t.ai_jobs)).all(), conn.execute(select(t.ai_usage_logs)).all()) == snapshot
    downloaded = api_client.get(f"/api/materials/{ids['materialId']}/file", headers=case['headers'])
    assert downloaded.status_code == 200 and downloaded.content == raw


@pytest.mark.parametrize('mode,ocr', [
    ('provider_failed', False), ('provider_timeout', False), ('bad_pages', False),
    ('malformed_json', False), ('ocr_failed', True)])
def test_failed_analysis_preserves_pdf_and_reupload_creates_a_new_attempt(
        api_client, material_case, domain_connection, worker, monkeypatch, mode, ocr):
    case, conn = material_case, domain_connection
    monkeypatch.setenv('MATERIAL_E2E_TRANSPORT', mode)
    raw = blank_pdf() if ocr else two_page_pdf()
    ids = upload(api_client, case, raw).json()['data']
    worker.run_job(case['user'], ids['jobId'])
    failed = job_view(api_client, case, ids['jobId'])
    assert failed['status'] == 'failed' and failed['ocr'] is None
    assert '목차' in failed['reason']
    assert not conn.execute(select(t.units)).first()
    assert not conn.execute(select(t.ai_saved_results)).first()
    first = dict(conn.execute(select(t.ai_jobs)).mappings().one())
    assert first['finished_at'] is not None
    assert conn.execute(select(t.ai_usage_logs)).first() is not None
    conn.commit()
    retry = upload(api_client, case, raw)
    assert retry.status_code == 200, retry.text
    new = retry.json()['data']
    assert new['materialId'] == ids['materialId'] and new['jobId'] != ids['jobId']
    monkeypatch.setenv('MATERIAL_E2E_TRANSPORT', 'ocr_good' if ocr else 'native')
    assert worker.run_job(case['user'], new['jobId'])
    assert job_view(api_client, case, new['jobId'])['status'] == 'done'
    assert dict(conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.id == ids['jobId'])).mappings().one()) == first


def test_structure_changed_during_analysis_is_not_overwritten(api_client, material_case, domain_connection, worker):
    from app.db.session import engine
    case, conn = material_case, domain_connection
    ids, _ = accepted(api_client, case)
    original_run = worker.gateway.run
    unit_id = str(uuid4())

    def edited_during_provider(request, **kwargs):
        outcome = original_run(request, **kwargs)
        with engine.begin() as editing:
            editing.execute(t.units.insert().values(id=unit_id, material_id=ids['materialId'], code='manual',
                title='User edit during analysis', sort_order=1, start_page=1, end_page=1,
                in_scope=False, created_at=now_utc(), updated_at=now_utc()))
        return outcome

    worker.gateway.run = edited_during_provider
    worker.run_job(case['user'], ids['jobId'])
    assert job_view(api_client, case, ids['jobId'])['status'] == 'failed'
    job = conn.execute(select(t.ai_jobs)).mappings().one()
    assert job['error_code'] == 'MATERIAL_STALE'
    unit = conn.execute(select(t.units)).mappings().one()
    assert unit['id'] == unit_id and unit['title'] == 'User edit during analysis' and not unit['in_scope']
    assert not conn.execute(select(t.concepts)).first()


def test_legacy_analysis_preserves_existing_unit_and_concept_ids(api_client, material_case, domain_connection, worker, monkeypatch):
    from tests.test_material_storage import add_file
    case, conn = material_case, domain_connection
    material_id, _, raw = add_file(conn, case, status='succeeded', create=False)
    unit_id, concept_id, now = str(uuid4()), str(uuid4()), now_utc()
    conn.execute(t.units.insert().values(id=unit_id, material_id=material_id, code='manual', title='Historical unit',
        sort_order=1, start_page=1, end_page=1, in_scope=False, created_at=now, updated_at=now))
    conn.execute(t.concepts.insert().values(id=concept_id, unit_id=unit_id, code='manual', title='Historical concept',
        sort_order=1, start_page=1, end_page=1, created_at=now, updated_at=now))
    previous = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all())
    conn.commit()
    ids = upload(api_client, case, raw).json()['data']
    assert ids['materialId'] == material_id
    monkeypatch.setenv('MATERIAL_E2E_TRANSPORT', 'ocr_good')
    assert worker.run_job(case['user'], ids['jobId'])
    assert job_view(api_client, case, ids['jobId'])['status'] == 'done'
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all()) == previous
    assert conn.scalar(select(t.ai_jobs.c.result))['structurePreserved'] is True


def test_expired_worker_cannot_finalize_after_reclaim(api_client, material_case, domain_connection, worker):
    from app.db.session import engine
    from app.services.material_analysis import MaterialAnalysisService, PROVIDER, VALIDATOR
    case, conn = material_case, domain_connection
    ids, _ = accepted(api_client, case)
    first = worker._claim(case['user'], ids['jobId'])
    assert first is not None
    assert not worker.run_job(case['user'], ids['jobId'])
    request, context = worker._prepare(first)
    outcome = worker.gateway.run(request, validator=VALIDATOR, provider=PROVIDER)
    future = now_utc() + timedelta(seconds=worker.settings.pdf_job_budget_seconds + 20)
    newer = MaterialAnalysisService(engine, storage_dir=case['root'], gateway=worker.gateway, clock=lambda: future)
    assert newer.run_job(case['user'], ids['jobId'])
    state = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all(), conn.execute(select(t.ai_jobs)).all())
    assert conn.scalar(select(t.ai_jobs.c.retry_count)) == 2
    conn.commit()
    assert worker._finish(first, outcome, context) is False
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all(), conn.execute(select(t.ai_jobs)).all()) == state
    assert job_view(api_client, case, ids['jobId'])['reused'] is True


def test_partial_structure_failure_rolls_back_then_retries_cached_analysis(api_client, material_case, domain_connection, worker):
    from sqlalchemy import event
    from app.db.session import engine
    case, conn = material_case, domain_connection
    ids, raw = accepted(api_client, case)

    def reject_concept(_conn, _cursor, _statement, _parameters, context, _many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'concepts':
            raise RuntimeError('Independent persistence failure after unit insertion')

    event.listen(engine, 'after_cursor_execute', reject_concept)
    try:
        worker.run_job(case['user'], ids['jobId'])
    finally:
        event.remove(engine, 'after_cursor_execute', reject_concept)
    failed = job_view(api_client, case, ids['jobId'])
    assert failed['status'] == 'failed'
    assert failed['failedStep'] == 2
    assert conn.execute(select(t.units)).first() is None and conn.execute(select(t.concepts)).first() is None
    assert conn.execute(select(t.ai_saved_results)).first() is not None
    usage = conn.execute(select(t.ai_usage_logs)).all()
    conn.commit()
    retry = upload(api_client, case, raw).json()['data']
    assert retry['jobId'] != ids['jobId']
    assert worker.run_job(case['user'], retry['jobId'])
    view = job_view(api_client, case, retry['jobId'])
    assert view['status'] == 'done' and view['reused'] is True
    assert conn.execute(select(t.ai_usage_logs)).all() == usage
    assert len(conn.execute(select(t.units)).all()) == len(conn.execute(select(t.concepts)).all()) == 1


def test_same_bytes_in_different_users_never_share_private_analysis(api_client, material_case, domain_connection, worker):
    case, conn = material_case, domain_connection
    first, raw = accepted(api_client, case)
    assert worker.run_job(case['user'], first['jobId'])
    other = {**case, 'subject': case['other_subject'], 'user': case['other_user'],
             'headers': {'X-Anonymous-Id': case['other_user']}}
    second = upload(api_client, other, raw).json()['data']
    assert worker.run_job(other['user'], second['jobId'])
    assert job_view(api_client, case, first['jobId'])['reused'] is False
    assert job_view(api_client, other, second['jobId'])['reused'] is False
    results = conn.execute(select(t.ai_saved_results)).mappings().all()
    assert len(results) == 2 and {row['user_id'] for row in results} == {case['user'], other['user']}
    assert len(conn.execute(select(t.ai_usage_logs)).all()) == 6


def test_missing_original_reports_reading_step_failure(api_client, material_case, domain_connection, worker):
    case, conn = material_case, domain_connection
    ids, _ = accepted(api_client, case)
    key = conn.scalar(select(t.materials.c.original_storage_key))
    conn.commit()
    (case['root'] / key).unlink()
    worker.run_job(case['user'], ids['jobId'])
    failed = job_view(api_client, case, ids['jobId'])
    assert failed['status'] == 'failed' and failed['ocr'] is None
    assert failed['failedStep'] == 0
    assert conn.execute(select(t.ai_usage_logs)).first() is None
