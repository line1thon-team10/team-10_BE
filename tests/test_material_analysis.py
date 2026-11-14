from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.gateway import GatewayResult
from app.ai.material_adapter import quality_report
from app.core.clock import now_utc
from app.db import schema as t
from app.repositories.ai_results import AiResultRepository
from app.services.material_analysis import MaterialAnalysisService
from tests.test_material_storage import material_case, pdf_bytes, upload


class FixtureGateway:
    def __init__(self, engine, *, method='native', readable=True, fail=False, callback=None):
        self.engine, self.method, self.readable, self.fail, self.callback = engine, method, readable, fail, callback
        self.calls = 0

    def run(self, request, **_):
        import json
        self.calls += 1
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError('private provider details must not escape')
        context = json.loads(request.identity.validation_json)
        pages = [{'page': index, 'text': 'Evidence text for material analysis ' * 5 if self.readable else ''}
                 for index in range(1, context['totalPages'] + 1)]
        value = {**context, 'schemaVersion': 'material-analysis-result-v1', 'method': self.method,
            'pages': pages, 'quality': quality_report(pages, context['totalPages']),
            'units': [{'title': 'Analysis', 'start_page': 1, 'end_page': context['totalPages'],
                'difficulty': 'mid', 'evidence': ['PDF content'],
                'concepts': [{'title': 'Concept', 'start_page': 1, 'end_page': 1}]}] if self.readable else []}
        saved = AiResultRepository(self.engine).save(request, value)
        return GatewayResult(value, saved, 'live', 1)


def service(case, **kwargs):
    from app.db.session import engine
    gateway = FixtureGateway(engine, **kwargs)
    return MaterialAnalysisService(engine, storage_dir=case['root'], gateway=gateway), gateway


@pytest.mark.parametrize('method,readable', [('native', True), ('ocr', True), ('ocr', False)])
def test_http_upload_worker_status_and_original(api_client, material_case, domain_connection, method, readable):
    case, conn, raw = material_case, domain_connection, pdf_bytes()
    uploaded = upload(api_client, case, raw).json()['data']
    route = f"/api/materials/jobs/{uploaded['jobId']}"
    queued = api_client.get(route, headers=case['headers'])
    assert queued.status_code == 200 and queued.json()['data']['status'] == 'running'
    assert queued.json()['data']['progress'] == 0 and queued.json()['data']['ocr'] is None
    worker, gateway = service(case, method=method, readable=readable)
    assert worker.run_next()
    finished = api_client.get(route, headers=case['headers']).json()['data']
    assert finished['status'] == 'done' and finished['progress'] == 100
    assert finished['ocr'] == (method == 'ocr')
    listing = api_client.get('/api/materials', headers=case['headers']).json()['data']['subjects']
    file = next(subject['files'][0] for subject in listing if subject['id'] == case['subject'])
    assert file['quizless'] is not readable and file['status'] == 'done'
    assert len(conn.execute(select(t.units)).all()) == int(readable)
    conn.commit()
    assert not worker.run_job(case['user'], uploaded['jobId']) and gateway.calls == 1
    assert api_client.get(f"/api/materials/{uploaded['materialId']}/file", headers=case['headers']).content == raw
    foreign = api_client.get(route, headers={'X-Anonymous-Id': case['other_user']})
    assert foreign.status_code == 404


def test_failure_is_durable_and_reupload_is_a_new_attempt(api_client, material_case, domain_connection):
    case, conn, raw = material_case, domain_connection, pdf_bytes()
    accepted = upload(api_client, case, raw).json()['data']
    worker, _ = service(case, fail=True)
    assert worker.run_next()
    result = api_client.get(f"/api/materials/jobs/{accepted['jobId']}", headers=case['headers']).json()['data']
    assert result['status'] == 'failed' and result['failedStep'] == 1
    assert result['ocr'] is None and 'private' not in result['reason']
    assert conn.execute(select(t.units)).first() is None
    conn.commit()
    retry = upload(api_client, case, raw).json()['data']
    assert retry['materialId'] == accepted['materialId'] and retry['jobId'] != accepted['jobId']
    worker, _ = service(case)
    assert worker.run_next()
    assert api_client.get(f"/api/materials/jobs/{retry['jobId']}", headers=case['headers']).json()['data']['status'] == 'done'


def test_structure_edit_during_analysis_is_preserved(api_client, material_case, domain_connection):
    case, conn = material_case, domain_connection
    accepted = upload(api_client, case, pdf_bytes()).json()['data']
    from app.db.session import engine
    def edit():
        with engine.begin() as writer:
            now = now_utc()
            writer.execute(t.units.insert().values(id=str(uuid4()), material_id=accepted['materialId'],
                code='manual', title='Manual edit', sort_order=1, start_page=1, end_page=1,
                created_at=now, updated_at=now))
    worker, _ = service(case, callback=edit)
    assert worker.run_next()
    assert conn.execute(select(t.units.c.title)).scalars().all() == ['Manual edit']
    job = conn.execute(select(t.ai_jobs)).mappings().one()
    assert job['status'] == 'failed' and job['error_code'] == 'MATERIAL_STALE'


def test_expired_claim_cannot_finish_after_reclaim(api_client, material_case, domain_connection):
    case = material_case
    accepted = upload(api_client, case, pdf_bytes()).json()['data']
    worker, gateway = service(case)
    clock = [now_utc()]
    worker.clock = lambda: clock[0]
    claim = worker._claim(case['user'], accepted['jobId'])
    request, context = worker._prepare(claim)
    assert not worker.run_job(case['user'], accepted['jobId'])
    clock[0] += timedelta(seconds=worker.settings.pdf_job_budget_seconds + 11)
    assert worker.run_job(case['user'], accepted['jobId'])
    assert not worker._finish(claim, gateway.run(request), context)
    conn = domain_connection
    job = conn.execute(select(t.ai_jobs)).mappings().one()
    assert job['status'] == 'succeeded' and job['retry_count'] == 2
    assert len(conn.execute(select(t.units)).all()) == 1
