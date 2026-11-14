"""Independent D-02 review: HTTP transaction boundaries and frozen DB facts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from app.core.responses import ApiError
from app.db import schema as t, session
from app.repositories.demo import DemoRepository
from app.services.daily_totals import DailyTotalsService
from app.services.planner import Planner
from tests.test_progress_contract import progress_world


def totals(w):
    return [dict(row) for row in w.conn.execute(select(t.daily_plan_totals).where(
        t.daily_plan_totals.c.user_id == w.user)).mappings()]


def service(w):
    user = dict(w.conn.execute(select(t.users).where(t.users.c.id == w.user)).mappings().one())
    return DailyTotalsService(w.conn, user)


def close(client, user, key=None):
    return client.post('/api/demo/next-day', headers={
        'X-Anonymous-Id': user, 'Idempotency-Key': key or str(uuid4())})


def prepare(w, end_focus):
    # Keep six remaining pages below the automatic recovery threshold.
    unit = w.unit(1, 1000)
    task = w.task((unit, 1, 10), done=[(unit, 1, 4)], status='partial')
    w.conn.commit()
    end_focus(w.user)
    return unit, task


def test_close_failure_rolls_back_denominator_and_carry_then_same_key_retries(
        api_client, progress_world, end_focus, monkeypatch):
    w = progress_world
    _, task = prepare(w, end_focus)
    before = totals(w)
    before_tasks = w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.plan_id == w.plan['id'])).all()
    w.conn.commit()
    key = str(uuid4())
    original = DemoRepository.save_request
    def failed_receipt(self, *args):
        original(self, *args)
        raise RuntimeError('independent failure after close and carry')
    monkeypatch.setattr(DemoRepository, 'save_request', failed_receipt)
    assert close(api_client, w.user, key).status_code == 500
    assert totals(w) == before
    assert w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.plan_id == w.plan['id'])).all() == before_tasks
    assert w.conn.scalar(select(t.users.c.demo_date).where(t.users.c.id == w.user)) == w.day
    w.conn.commit()
    monkeypatch.setattr(DemoRepository, 'save_request', original)
    result = close(api_client, w.user, key)
    assert result.status_code == 200, result.text
    assert service(w).view(w.day)['plan'] == 10
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == task)) == 'carried_over'


def test_cross_task_pdf_union_is_distinct_from_task_denominator(api_client, progress_world, end_focus):
    w = progress_world
    first = w.unit(1, 10)
    same_pdf = w.unit(5, 15, material_id=first['material_id'])
    other_pdf = w.unit(1, 10)
    w.task((first, 1, 10))
    w.task((same_pdf, 5, 15))
    w.task((other_pdf, 1, 10))
    w.task((first, 1, 5), kind='remedial')
    w.task(kind='review')
    w.conn.commit()
    end_focus(w.user)
    result = close(api_client, w.user)
    assert result.status_code == 200, result.text
    frozen = next(r for r in totals(w) if r['subject_id'] == w.subject['id'] and r['study_date'] == w.day)
    assert frozen['planned_total_pages'] == 36  # placement achievement retains repeats
    assert frozen['planned_unpaged_minutes'] == 25
    assert frozen['planned_new_pages'] == 25  # same-PDF union 1..15 + other-PDF 1..10
    w.conn.commit()
    assert service(w).snapshot(w.day, finalize=True, at=w.now + timedelta(days=1))[w.subject['id']] == frozen


def test_legacy_finalized_denominator_preserves_all_columns_after_correction(
        api_client, progress_world, end_focus):
    w = progress_world
    _, task = prepare(w, end_focus)
    w.conn.execute(t.daily_plan_totals.delete().where(t.daily_plan_totals.c.user_id == w.user))
    w.conn.execute(t.daily_plan_totals.insert().values(user_id=w.user, subject_id=w.subject['id'],
        study_date=w.day, plan_id=w.plan['id'], planned_new_pages=7, planned_total_pages=12,
        planned_unpaged_minutes=9, finalized_at=w.now, updated_at=w.now))
    w.conn.commit()
    assert close(api_client, w.user).status_code == 200
    before = totals(w)
    w.conn.commit()
    response = api_client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': w.user}, json={'toPage': 2})
    assert response.status_code == 200, response.text
    view = service(w).view(w.day)
    assert (view['plan'], view['done']) == (12, 2)
    assert totals(w) == before
    assert service(w).snapshot(w.day, finalize=True, at=w.now + timedelta(days=1))[w.subject['id']] == before[0]


def test_close_lock_rejects_concurrent_record_correction_then_retry_preserves_denominator(
        api_client, progress_world, end_focus, monkeypatch):
    w = progress_world
    _, task = prepare(w, end_focus)
    if w.conn.dialect.name == 'sqlite':
        @event.listens_for(session.engine, 'checkout')
        def short_busy(connection, *args):
            connection.execute('PRAGMA busy_timeout=20')
    entered, release = Event(), Event()
    original = Planner.advance_day
    def held(self):
        entered.set()
        assert release.wait(5)
        return original(self)
    monkeypatch.setattr(Planner, 'advance_day', held)
    def correct():
        return api_client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': w.user}, json={'toPage': 8})
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(close, api_client, w.user)
        try:
            assert entered.wait(5)
            raced = correct()
            assert raced.status_code == 409, raced.text
            assert raced.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
    before = totals(w)
    w.conn.commit()
    retried = correct()
    assert retried.status_code == 200, retried.text
    assert totals(w) == before
    assert (service(w).view(w.day)['plan'], service(w).view(w.day)['done']) == (10, 8)


def test_foreign_correction_cannot_change_closed_owner_snapshot(api_client, progress_world, end_focus):
    w = progress_world
    _, task = prepare(w, end_focus)
    assert close(api_client, w.user).status_code == 200
    before = totals(w)
    w.conn.commit()
    other = api_client.post('/api/session').json()['data']['anonymousId']
    rejected = api_client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': other}, json={'toPage': 9})
    assert rejected.status_code == 404, rejected.text
    assert totals(w) == before
    assert service(w).view(w.day)['done'] == 4


@pytest.mark.parametrize('zone,local_midnight', [
    ('Asia/Seoul', datetime(2026, 11, 8, 15, tzinfo=timezone.utc)),
    ('America/New_York', datetime(2026, 11, 9, 5, tzinfo=timezone.utc)),
])
def test_timezone_midnight_controls_future_visibility_without_read_writes(progress_world, zone, local_midnight):
    w = progress_world
    unit = w.unit(1, 10)
    w.task((unit, 1, 10), done=[(unit, 1, 10)], status='completed')
    w.conn.execute(update(t.users).where(t.users.c.id == w.user).values(demo_date=None, timezone=zone))
    w.conn.commit()
    svc = service(w)
    original = totals(w)
    assert svc.view(w.day, at=local_midnight - timedelta(microseconds=1))['done'] == 0
    with pytest.raises(ApiError) as caught:
        svc.snapshot(w.day, finalize=True, at=local_midnight - timedelta(microseconds=1))
    assert caught.value.code == 'INVALID_INPUT'
    assert totals(w) == original
    assert svc.view(w.day, at=local_midnight)['done'] == 10
    assert totals(w) == original
    finalized = svc.snapshot(w.day, finalize=True, at=local_midnight)
    assert finalized[w.subject['id']]['planned_total_pages'] == 10


def test_close_does_not_count_records_excluded_before_snapshot(api_client, progress_world, end_focus):
    w = progress_world
    unit, _ = prepare(w, end_focus)
    excluded = w.task((unit, 11, 20), done=[(unit, 11, 20)], status='excluded')
    w.conn.commit()
    assert (service(w).view(w.day)['plan'], service(w).view(w.day)['done']) == (10, 4)
    w.conn.commit()
    response = close(api_client, w.user)
    assert response.status_code == 200, response.text
    assert service(w).view(w.day)['plan'] == 10
    assert service(w).view(w.day)['done'] == 4
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == excluded)) == 'excluded'
