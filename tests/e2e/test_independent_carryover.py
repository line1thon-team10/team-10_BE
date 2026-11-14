"""Independent L-01: actual HTTP closes, bounded slots and correction lineage."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from app.db import schema as t, session
from app.services.planner import Planner
from tests.test_progress_contract import progress_world
from tests.test_onboarding_events import add_event


def headers(w): return {'X-Anonymous-Id': w.user}


def close(client, w):
    return client.post('/api/demo/next-day', headers={**headers(w), 'Idempotency-Key': str(uuid4())})


def setup(w, end_focus, *, minutes=40, done=4, next_offset=1, busy_from=None):
    unit = w.unit(1, 1000)
    root = w.task((unit, 1, 10), kind='remedial', done=[(unit, 1, done)] if done else [], status='partial' if done else 'planned')
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == root).values(estimated_minutes=minutes))
    marker = w.task(kind='review', day=w.day + timedelta(days=next_offset))
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == marker).values(estimated_minutes=15))
    w.conn.execute(update(t.calendar_events).where(t.calendar_events.c.user_id == w.user).values(deleted_at=w.now))
    if busy_from:
        owner = dict(w.conn.execute(select(t.users).where(t.users.c.id == w.user)).mappings().one())
        add_event(w.conn, owner, day=w.day + timedelta(days=next_offset), start=busy_from, end='23:00')
    w.conn.commit()
    end_focus(w.user)
    return root, marker


def task(w, identifier):
    return dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == identifier)).mappings().one())


def children(w, root):
    return [dict(r) for r in w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id == root)).mappings()]


def test_correction_cannot_expand_carry_past_actual_remaining_slot(api_client, progress_world, end_focus):
    w = progress_world
    root, _ = setup(w, end_focus, busy_from='08:40')
    response = close(api_client, w)
    assert response.status_code == 200, response.text
    child = children(w, root)[0]
    assert child['estimated_minutes'] == 24
    # 08:00..08:15 is occupied; only 25 minutes remain before the fixed event.
    assert child['starts_at'] is not None and child['ends_at'] is not None
    w.conn.commit()
    changed = api_client.patch(f'/api/tasks/{root}/record', headers=headers(w), json={'toPage': 2})
    assert changed.status_code == 200, changed.text
    assert task(w, child['id']) == child, [(r['estimated_minutes'], r['starts_at'], r['ends_at']) for r in children(w, child['id'])]
    assert children(w, child['id']) == []
    record = w.conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == root))
    assert w.conn.scalar(select(t.study_record_ranges.c.end_page).where(t.study_record_ranges.c.record_id == record)) == 2


@pytest.mark.parametrize('minutes,expected', [(14, False), (15, True), (60, True), (61, False)])
def test_boundary_duration_and_next_subject_study_day_are_respected(api_client, progress_world, end_focus, minutes, expected):
    w = progress_world
    root, _ = setup(w, end_focus, minutes=minutes, done=0, next_offset=2)
    response = close(api_client, w)
    assert response.status_code == 200, response.text
    copied = children(w, root)
    assert bool(copied) is expected
    if expected:
        assert len(copied) == 1
        assert copied[0]['scheduled_date'] == w.day + timedelta(days=2)
        assert copied[0]['estimated_minutes'] == minutes
        assert copied[0]['starts_at'] and copied[0]['ends_at']
        assert task(w, root)['status'] == 'carried_over'
    else:
        assert task(w, root)['status'] in ('planned', 'partial', 'missed')
        assert w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == root)).first()


def test_started_real_child_and_ranges_are_immutable_after_parent_completion(api_client, progress_world, end_focus):
    w = progress_world
    root, _ = setup(w, end_focus)
    assert close(api_client, w).status_code == 200
    child = children(w, root)[0]
    before_ranges = w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == child['id'])).all()
    w.conn.commit()
    started = api_client.post('/api/study/sessions', headers=headers(w), json={'taskId': child['id']})
    assert started.status_code == 200, started.text
    changed = api_client.patch(f'/api/tasks/{root}/record', headers=headers(w), json={'toPage': 10})
    assert changed.status_code == 200, changed.text
    assert task(w, child['id']) == child
    assert children(w, child['id']) == []
    assert w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == child['id'])).all() == before_ranges
    active = api_client.get('/api/study/sessions/active', headers=headers(w))
    assert active.status_code == 200 and active.json()['data']['taskId'] == child['id']


def test_close_and_historical_correction_serialize_before_child_reconciliation(api_client, progress_world, end_focus, monkeypatch):
    w = progress_world
    root, _ = setup(w, end_focus)
    if w.conn.dialect.name == 'sqlite':
        @event.listens_for(session.engine, 'checkout')
        def quick(connection, *args): connection.execute('PRAGMA busy_timeout=20')
    entered, release = Event(), Event()
    original = Planner.advance_day
    def held(self):
        entered.set()
        assert release.wait(5)
        return original(self)
    monkeypatch.setattr(Planner, 'advance_day', held)
    def correct(): return api_client.patch(f'/api/tasks/{root}/record', headers=headers(w), json={'toPage': 6})
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(close, api_client, w)
        try:
            assert entered.wait(5)
            rejected = correct()
            assert rejected.status_code == 409 and rejected.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
    original_child = children(w, root)[0]
    w.conn.commit()
    assert correct().status_code == 200
    replacement = children(w, original_child['id'])
    assert len(replacement) == 1 and replacement[0]['estimated_minutes'] == 16
    assert replacement[0]['starts_at'] is not None and replacement[0]['ends_at'] is not None
    assert replacement[0]['ends_at'] - replacement[0]['starts_at'] == timedelta(minutes=16)
    assert task(w, original_child['id'])['status'] == 'carried_over'


def test_exam_day_marker_never_authorizes_carry_or_removes_remedial_ranges(api_client, progress_world, end_focus):
    w = progress_world
    root, marker = setup(w, end_focus, next_offset=2)
    w.conn.execute(update(t.subjects).where(t.subjects.c.id == w.subject['id']).values(exam_date=w.day + timedelta(days=2)))
    before = w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == root)).all()
    w.conn.commit()
    response = close(api_client, w)
    assert response.status_code == 200, response.text
    assert children(w, root) == []
    assert task(w, root)['status'] == 'partial'
    assert w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == root)).all() == before
