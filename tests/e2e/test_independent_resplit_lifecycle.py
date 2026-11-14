"""Independent approval transaction and historical correction boundaries."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import event, select, update

from app.db import schema as t, session
from tests.test_plan_draft import NOW, TODAY
from tests.test_plan_resplit_e2e import plan_case, addition_case, call, prepare, state
from tests.e2e.test_independent_resplit_grounding import model, ready


def apply(client, case, identifier):
    return call(client, case, 'post', '/api/plan/resplit/apply', json={'previewId': identifier})


def test_selected_initial_draft_approval_rolls_back_both_selection_and_placements(api_client, domain_connection, plan_case, model):
    conn, case = domain_connection, plan_case
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['active']).values(status='archived')); conn.commit()
    old = call(api_client, case, 'get', '/api/plan/draft').json()['data']['previewId']
    model(ready())
    result = prepare(api_client, case, request='내일부터 하루30분까지 다시 나눠줘')
    assert result.status_code == 200, result.text
    identifier = result.json()['data']['previewId']
    before = state(conn); conn.commit()

    def fault(connection, cursor, sql, *args):
        if sql.startswith('INSERT INTO study_tasks '):
            raise RuntimeError('independent approval fault')
    event.listen(session.engine, 'before_cursor_execute', fault)
    try:
        failed = call(api_client, case, 'post', '/api/plan/approve', json={'previewId': identifier})
    finally:
        event.remove(session.engine, 'before_cursor_execute', fault)
    assert failed.status_code == 500, failed.text
    assert state(conn) == before; conn.commit()
    assert call(api_client, case, 'get', '/api/plan/draft').json()['data']['previewId'] == old
    assert apply(api_client, case, identifier).status_code == 200
    assert state(conn) == before; conn.commit()
    replacement = call(api_client, case, 'get', '/api/plan/draft').json()['data']['previewId']
    assert replacement != old
    stale = call(api_client, case, 'post', '/api/plan/approve', json={'previewId': old})
    assert stale.status_code == 409, stale.text
    approved = call(api_client, case, 'post', '/api/plan/approve', json={'previewId': identifier})
    assert approved.status_code == 200, approved.text
    after = state(conn); conn.commit()
    assert call(api_client, case, 'post', '/api/plan/approve', json={'previewId': replacement}).status_code == 200
    assert state(conn) == after
    assert conn.scalar(select(t.plans.c.revision)) == 1
    rows = conn.execute(select(t.study_tasks)).mappings().all()
    assert len(rows) == 2 and all(row['scheduled_date'] > TODAY and row['estimated_minutes'] <= 30 for row in rows)


def test_historical_http_correction_invalidates_preview_and_rebuild_keeps_corrected_evidence(api_client, domain_connection, addition_case, model):
    conn, case = domain_connection, addition_case
    old = case['old_task']; record, focus = str(uuid4()), str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == old['id']).values(
        scheduled_date=TODAY-timedelta(days=1), status='partial', starts_at=None, ends_at=None))
    conn.execute(t.study_sessions.insert().values(id=focus, user_id=case['user'], task_id=old['id'], status='ended',
        started_at=NOW-timedelta(days=1), ended_at=NOW-timedelta(days=1)+timedelta(minutes=15),
        measured_seconds=900, study_date=TODAY-timedelta(days=1), created_at=NOW))
    conn.execute(t.study_records.insert().values(id=record, task_id=old['id'], session_id=focus, result='partial',
        study_date=TODAY-timedelta(days=1), recorded_at=NOW, updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record,
        unit_id=case['unit'], start_page=1, end_page=5)); conn.commit()
    model(ready())
    candidate = prepare(api_client, case, request='내일부터 하루30분까지 다시 나눠줘')
    assert candidate.status_code == 200, candidate.text
    correction = call(api_client, case, 'patch', f"/api/tasks/{old['id']}/record", json={'toPage': 8})
    assert correction.status_code == 200, correction.text
    corrected = state(conn); conn.commit()
    assert apply(api_client, case, candidate.json()['data']['previewId']).status_code == 409
    assert state(conn) == corrected; conn.commit()
    rebuilt = call(api_client, case, 'post', '/api/plan/rebuild', json={})
    assert rebuilt.status_code == 200, rebuilt.text
    applied = apply(api_client, case, rebuilt.json()['data']['previewId'])
    assert applied.status_code == 200, applied.text
    after = state(conn)
    for name in ['study_records', 'study_record_ranges', 'study_sessions', 'learning_passes']:
        assert after[name] == corrected[name]
    rows = conn.execute(select(t.study_task_ranges).join(t.study_tasks,
        t.study_tasks.c.id == t.study_task_ranges.c.task_id).where(t.study_tasks.c.status == 'planned')).mappings().all()
    assert sorted(page for row in rows for page in range(row['start_page'], row['end_page']+1)) == list(range(9, 21))
