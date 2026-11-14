"""Real upload/worker/DB/quiz API, replacing only provider results."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.ai.gateway import GatewayResult
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t, session
from app.repositories.ai_results import AiResultRepository
from app.services.material_analysis import MaterialAnalysisService
from tests.test_material_analysis import FixtureGateway
from tests.test_material_storage import material_case, pdf_bytes, upload


class BankGateway:
    def __init__(self):
        self.calls, self.fail, self.callback, self.foreign = 0, False, None, None

    def run(self, request, **kwargs):
        self.calls += 1
        context = json.loads(request.identity.validation_json)
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError('injected provider outage')
        types = ['multiple_choice'] * 6 + ['short_answer'] * 3 + ['essay']
        value = {'questions': [{'id': i, 'type': kind,
            'difficulty': {'low': 'easy', 'mid': 'medium', 'high': 'hard'}[context['difficulty']],
            'question': f'원문의 Evidence 표현 확인 {i}', 'answer': 'Evidence',
            'explanation': '원문에 Evidence text가 적혀 있습니다.',
            'options': ['Evidence', 'Ocean', 'Cloud', 'Tree'] if kind == 'multiple_choice' else [],
            'concept_id': self.foreign or context['unit']['concepts'][0]['id'], 'evidence_id': 1,
            'keywords': ['Evidence']} for i, kind in enumerate(types, 1)]}
        saved = AiResultRepository(session.engine).save(request, value)
        return GatewayResult(value, saved, 'live', 1)


@pytest.fixture
def bank_world(api_client, domain_connection, material_case):
    from app.services.question_bank import QuestionBankService
    conn, case = domain_connection, material_case
    uploaded = upload(api_client, case, pdf_bytes('Evidence text for material analysis.')).json()['data']
    analysis = MaterialAnalysisService(session.engine, storage_dir=case['root'], gateway=FixtureGateway(session.engine))
    assert analysis.run_next()
    unit = dict(conn.execute(select(t.units)).mappings().one())
    today, now = date(2026, 11, 16), now_utc()
    plan, block, task = [str(uuid4()) for _ in range(3)]
    conn.execute(update(t.users).where(t.users.c.id == case['user']).values(demo_date=today))
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['subject']).values(status='active', exam_date=today+timedelta(days=3)))
    conn.execute(t.plans.insert().values(id=plan, user_id=case['user'], revision=1, created_at=now, approved_at=now))
    conn.execute(t.study_blocks.insert().values(id=block, subject_id=case['subject'], unit_id=unit['id'], task_type='study', created_at=now))
    conn.execute(t.study_tasks.insert().values(id=task, plan_id=plan, subject_id=case['subject'], block_id=block,
        task_type='study', title='자료 학습', scheduled_date=today, estimated_minutes=15, status='planned', created_at=now))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=task, unit_id=unit['id'], start_page=1, end_page=1, sort_order=1)); conn.commit()
    recorded = api_client.post('/api/records', headers=case['headers'], json={'taskId': task, 'result': 'done'})
    assert recorded.status_code == 200, recorded.text
    gateway = BankGateway()
    bank = QuestionBankService(session.engine, storage_dir=case['root'], gateway=gateway)
    return {**case, **uploaded, 'unit': unit, 'gateway': gateway, 'worker': bank}


def count(conn, table):
    return conn.scalar(select(func.count()).select_from(table))


def quiz(client, world):
    return client.get(f"/api/quiz/{world['subject']}", headers=world['headers'])


def test_bank_is_bounded_and_daily_plus_extra_have_distinct_five(api_client, domain_connection, bank_world):
    conn, w = domain_connection, bank_world
    for _ in range(3):
        assert quiz(api_client, w).status_code == 404
    assert w['gateway'].calls == 0 and count(conn, t.questions) == 0; conn.commit()
    assert w['worker'].run_next()
    assert count(conn, t.questions) == 10; conn.commit()
    daily = quiz(api_client, w)
    assert daily.status_code == 200, daily.text
    data = daily.json()['data']
    assert len(data['questions']) == 5
    for _ in range(3):
        assert quiz(api_client, w).json() == daily.json()
    submitted = api_client.post(f"/api/quiz/{data['quizId']}/submit", headers=w['headers'], json={
        'quizId': data['quizId'], 'answers': [{'questionId': q['id'], 'choice': 0, 'confidence': 'sure'} for q in data['questions']]})
    assert submitted.status_code == 200, submitted.text
    more = api_client.post(f"/api/quiz/{w['subject']}/more", headers=w['headers'], json={'unitId': w['unit']['id']})
    assert more.status_code == 200 and more.json()['data']['count'] == 5, more.text
    ids = list(conn.scalars(select(t.quiz_items.c.question_id)))
    assert len(ids) == len(set(ids)) == 10
    quotes = list(conn.scalars(select(t.question_sources.c.quote)))
    analysis = conn.scalar(select(t.ai_jobs.c.result).where(t.ai_jobs.c.id == w['jobId']))['analysis']
    assert all(quote in analysis['pages'][0]['text'] for quote in quotes)
    assert w['gateway'].calls == 1
    conn.commit()
    assert w['worker'].run_next() and w['worker'].run_next()
    assert not w['worker'].run_next()
    assert w['gateway'].calls == 3 and count(conn, t.questions) == 30


def test_failed_bank_does_not_damage_analysis_or_retry_through_get(api_client, domain_connection, bank_world):
    conn, w = domain_connection, bank_world
    w['gateway'].fail = True
    for _ in range(3):
        assert w['worker'].run_next()
    assert not w['worker'].run_next()
    failed = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.kind == 'question_pregenerate')).mappings().all()
    assert len(failed) == 3 and all(row['status'] == 'failed' for row in failed)
    assert conn.scalar(select(t.materials.c.analysis_status)) == 'succeeded'
    assert conn.scalar(select(t.ai_jobs.c.status).where(t.ai_jobs.c.id == w['jobId'])) == 'succeeded'
    assert count(conn, t.questions) == 0; conn.commit()
    for _ in range(3):
        assert quiz(api_client, w).status_code == 404
    assert w['gateway'].calls == 3
    low = next(row for row in failed if row['input_payload']['difficulty'] == 'low')
    with pytest.raises(ApiError) as foreign:
        w['worker'].retry(w['other_user'], low['id'])
    assert foreign.value.status == 404
    w['gateway'].fail = False
    assert w['worker'].retry(w['user'], low['id'])
    assert w['worker'].run_job(w['user'], low['id'])
    assert not w['worker'].run_job(w['user'], low['id'])
    assert count(conn, t.questions) == 10 and w['gateway'].calls == 4


def test_concurrent_worker_has_one_claim_and_version_change_during_ai_cannot_publish(api_client, domain_connection, bank_world):
    conn, w = domain_connection, bank_world
    user, job = w['worker']._discover(lambda: False)
    entered, release = Event(), Event()
    def gate():
        entered.set()
        assert release.wait(10)
    w['gateway'].callback = gate
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(w['worker'].run_job, user, job)
        assert entered.wait(10)
        assert not w['worker'].run_job(user, job)
        with session.engine.begin() as writer:
            writer.execute(update(t.materials).where(t.materials.c.id == w['materialId']).values(analysis_version='changed-during-provider'))
        release.set()
        assert first.result(10)
    assert w['gateway'].calls == 1 and count(conn, t.questions) == 0
    row = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.id == job)).mappings().one()
    assert row['status'] == 'failed' and row['error_code'] == 'QUESTION_BANK_STALE'


@pytest.mark.parametrize('reason', ['quizless', 'no_evidence', 'retired'])
def test_ineligible_material_never_admits_bank_work(api_client, domain_connection, bank_world, reason):
    conn, w = domain_connection, bank_world
    if reason == 'quizless':
        conn.execute(update(t.materials).where(t.materials.c.id == w['materialId']).values(quiz_enabled=False))
    else:
        conn.execute(update(t.units).where(t.units.c.id == w['unit']['id']).values(
            **({'evidence': []} if reason == 'no_evidence' else {'retired_at': now_utc()})))
    conn.commit()
    assert not w['worker'].run_next()
    assert w['gateway'].calls == 0 and count(conn, t.questions) == 0
