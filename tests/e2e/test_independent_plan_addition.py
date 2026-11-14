"""Independent remaining-work preservation when a draft subject joins a plan."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update

from app.db import schema as t
from tests.test_plan_addition_e2e import addition_case
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_draft_e2e import approve, draft


def test_past_partial_record_with_consumed_focus_session_keeps_remaining_pages(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    task = case['old_task']
    yesterday = TODAY - timedelta(days=1)
    session, record = str(uuid4()), str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task['id']).values(
        scheduled_date=yesterday, starts_at=None, ends_at=None, status='partial'))
    conn.execute(t.study_sessions.insert().values(id=session, user_id=case['user'], task_id=task['id'],
        status='ended', started_at=NOW - timedelta(days=1), ended_at=NOW - timedelta(days=1) + timedelta(minutes=15),
        measured_seconds=900, study_date=yesterday, created_at=NOW - timedelta(days=1)))
    conn.execute(t.study_records.insert().values(id=record, task_id=task['id'], session_id=session, result='partial',
        study_date=yesterday, recorded_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1)))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record,
        unit_id=case['unit'], start_page=1, end_page=5))
    evidence = {table.name: conn.execute(select(table)).all()
                for table in (t.study_sessions, t.study_records, t.study_record_ranges)}
    conn.commit()
    response = draft(api_client, case, draftSubjectIds=case['added'])
    assert response.status_code == 200, response.text
    applied = approve(api_client, case, response.json()['data']['previewId'])
    assert applied.status_code == 200, applied.text
    remaining = conn.execute(select(t.study_task_ranges).join(t.study_tasks,
        t.study_tasks.c.id == t.study_task_ranges.c.task_id).where(
        t.study_tasks.c.subject_id == case['subject'], t.study_tasks.c.task_type == 'study',
        t.study_tasks.c.status == 'planned', t.study_tasks.c.scheduled_date >= TODAY)).mappings().all()
    assert sorted(page for row in remaining for page in range(row['start_page'], row['end_page'] + 1)) == list(range(6, 21))
    assert {table.name: conn.execute(select(table)).all()
            for table in (t.study_sessions, t.study_records, t.study_record_ranges)} == evidence


def test_new_focus_session_after_addition_preview_invalidates_approval(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    preview = draft(api_client, case, draftSubjectIds=case['added']).json()['data']['previewId']
    session = str(uuid4())
    conn.execute(t.study_sessions.insert().values(id=session, user_id=case['user'], task_id=case['old_task']['id'],
        status='active', started_at=NOW, study_date=TODAY, created_at=NOW))
    before = {table.name: conn.execute(select(table).order_by(table.c.id)).all()
              for table in (t.plans, t.subjects, t.study_tasks, t.study_sessions)}
    conn.commit()
    result = approve(api_client, case, preview)
    assert result.status_code == 409 and result.json()['error']['code'] == 'PREVIEW_STALE', result.text
    assert {table.name: conn.execute(select(table).order_by(table.c.id)).all()
            for table in (t.plans, t.subjects, t.study_tasks, t.study_sessions)} == before


def test_addition_range_failure_restores_superseded_tasks_and_draft_status(api_client, domain_connection, addition_case):
    from sqlalchemy import event
    from app.db.session import engine
    conn, case = domain_connection, addition_case
    preview = draft(api_client, case, draftSubjectIds=case['added']).json()['data']['previewId']
    tables = (t.plans, t.subjects, t.study_blocks, t.study_tasks, t.study_task_ranges)
    before = {table.name: conn.execute(select(table).order_by(table.c.id)).all() for table in tables}
    conn.commit()

    def fail_range(_conn, _cursor, _statement, _parameters, context, _many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'study_task_ranges':
            raise RuntimeError('Independent addition failure after superseding placements')

    event.listen(engine, 'after_cursor_execute', fail_range)
    try:
        failed = approve(api_client, case, preview)
    finally:
        event.remove(engine, 'after_cursor_execute', fail_range)
    assert failed.status_code == 500
    assert {table.name: conn.execute(select(table).order_by(table.c.id)).all() for table in tables} == before
    conn.commit()
    assert approve(api_client, case, preview).status_code == 200
    assert conn.scalar(select(t.plans.c.revision)) == 2
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id == case['added'])) == 'active'
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == case['old_task']['id'])) == 'carried_over'
    after = {table.name: conn.execute(select(table).order_by(table.c.id)).all() for table in tables}
    conn.commit()
    assert approve(api_client, case, preview).status_code == 200
    assert {table.name: conn.execute(select(table).order_by(table.c.id)).all() for table in tables} == after
