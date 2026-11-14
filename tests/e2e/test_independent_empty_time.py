"""Independent C-04 full HTTP release -> choice -> persisted placement tests."""
from datetime import timedelta
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_onboarding_events import add_event
from tests.e2e.test_schedule_delete import user, preview, apply as delete_event
from tests.e2e.test_empty_time import apply as choose


def release(client, w, event):
    w.conn.commit()
    key = f"{event['id']}@{w.day}"
    candidate = preview(client, w, key)
    assert candidate.status_code == 200, candidate.text
    result = delete_event(client, w, key, candidate.json()['data']['previewId'])
    assert result.status_code == 200, result.text
    return result.json()['data']


def test_real_overlap_release_pulls_whole_task_once_into_exact_fit(api_client, progress_world):
    w = progress_world
    unit = w.unit(1, 30)
    task = w.task((unit, 1, 6), day=w.day + timedelta(days=1))
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task).values(estimated_minutes=15))
    event = add_event(w.conn, user(w), day=w.day, start='12:00', end='13:00')
    add_event(w.conn, user(w), day=w.day, start='12:15', end='12:45')
    candidate = release(api_client, w, event)
    assert [(s['start'], s['end']) for s in candidate['slots']] == [('12:00', '12:15'), ('12:45', '13:00')]
    assert next(o for o in candidate['options'] if o['key'] == 'pull')['enabled']
    result = choose(api_client, w, candidate, 'pull')
    assert result.status_code == 200, result.text
    rows = w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id == task)).mappings().all()
    assert len(rows) == 1 and rows[0]['estimated_minutes'] == 15
    assert rows[0]['scheduled_date'] == w.day
    span = w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == rows[0]['id'])).mappings().one()
    assert (span['start_page'], span['end_page']) == (1, 6)
    w.conn.commit()
    assert choose(api_client, w, candidate, 'pull').json() == result.json()
    assert len(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id == task)).all()) == 1


def test_apply_failure_rolls_back_move_revision_and_preview_can_retry(api_client, progress_world, monkeypatch):
    import app.services.empty_time as empty
    w = progress_world
    unit = w.unit(1, 10)
    task = w.task((unit, 1, 10), day=w.day + timedelta(days=1))
    event = add_event(w.conn, user(w), day=w.day, start='12:00', end='13:00')
    candidate = release(api_client, w, event)
    original_tasks = w.conn.execute(select(t.study_tasks)).all()
    original_plan = w.conn.execute(select(t.plans).where(t.plans.c.id == w.plan['id'])).one()
    w.conn.commit()
    real = empty.apply_saved_placements
    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('independent failure after saved placements')
    monkeypatch.setattr(empty, 'apply_saved_placements', fail)
    assert choose(api_client, w, candidate, 'pull').status_code == 500
    assert w.conn.execute(select(t.study_tasks)).all() == original_tasks
    assert w.conn.execute(select(t.plans).where(t.plans.c.id == w.plan['id'])).one() == original_plan
    w.conn.commit()
    monkeypatch.setattr(empty, 'apply_saved_placements', real)
    result = choose(api_client, w, candidate, 'pull')
    assert result.status_code == 200, result.text
    assert len(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id == task)).all()) == 1


def test_record_change_invalidates_offer_even_without_manual_plan_revision(api_client, progress_world):
    w = progress_world
    unit = w.unit(1, 20)
    today = w.task((unit, 1, 10), done=[], status='planned')
    future = w.task((unit, 11, 20), day=w.day + timedelta(days=1))
    event = add_event(w.conn, user(w), day=w.day, start='12:00', end='13:00')
    candidate = release(api_client, w, event)
    response = api_client.patch(f'/api/tasks/{today}/record', headers={'X-Anonymous-Id': w.user}, json={'toPage': 5})
    assert response.status_code == 200, response.text
    stale = choose(api_client, w, candidate, 'pull')
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'PREVIEW_STALE'
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == future)) == 'planned'
    assert w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id == future)).all() == []
