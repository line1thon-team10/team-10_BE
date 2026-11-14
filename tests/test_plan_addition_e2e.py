"""Adding selected draft subjects preserves active plan facts and obligations."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_draft_e2e import approve, draft


@pytest.fixture
def addition_case(api_client, domain_connection, plan_case):
    conn, case = domain_connection, plan_case
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['active']).values(status='archived'))
    conn.commit()
    first = draft(api_client, case).json()['data']['previewId']
    assert approve(api_client, case, first).status_code == 200
    original = dict(conn.execute(select(t.study_tasks)).mappings().one())
    row = dict(conn.execute(select(t.subjects).where(t.subjects.c.id == case['subject'])).mappings().one())
    added, material, unit = (str(uuid4()) for _ in range(3))
    conn.execute(t.subjects.insert().values(**{**row, 'id': added, 'code': added, 'status': 'draft', 'name': '새 과목'}))
    source = dict(conn.execute(select(t.materials).where(t.materials.c.id == case['material'])).mappings().one())
    conn.execute(t.materials.insert().values(**{**source, 'id': material, 'subject_id': added, 'page_count': 6}))
    source = dict(conn.execute(select(t.units).where(t.units.c.id == case['unit'])).mappings().one())
    conn.execute(t.units.insert().values(**{**source, 'id': unit, 'material_id': material, 'end_page': 6}))
    conn.commit()
    return {**case, 'added': added, 'added_material': material, 'added_unit': unit, 'old_task': original}


@pytest.mark.parametrize('consumed_focus', [False, True])
def test_addition_preserves_recorded_pages_and_unmoved_remedial(api_client, domain_connection, addition_case, consumed_focus):
    conn, case = domain_connection, addition_case
    task = case['old_task']
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task['id']).values(
        scheduled_date=TODAY - timedelta(days=1), starts_at=None, ends_at=None, status='partial'))
    record, session_id = str(uuid4()), str(uuid4()) if consumed_focus else None
    if session_id:
        conn.execute(t.study_sessions.insert().values(id=session_id, user_id=case['user'], task_id=task['id'],
            status='ended', started_at=NOW-timedelta(days=1), ended_at=NOW-timedelta(days=1)+timedelta(minutes=15),
            measured_seconds=900, study_date=TODAY-timedelta(days=1), created_at=NOW))
    conn.execute(t.study_records.insert().values(id=record, task_id=task['id'], session_id=session_id, result='partial',
        study_date=TODAY - timedelta(days=1), recorded_at=NOW, updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record,
        unit_id=case['unit'], start_page=1, end_page=5))
    remedial, block = str(uuid4()), str(uuid4())
    conn.execute(t.study_blocks.insert().values(id=block, subject_id=case['subject'], unit_id=case['unit'],
        task_type='remedial', created_at=NOW))
    conn.execute(t.study_tasks.insert().values(id=remedial, plan_id=task['plan_id'], subject_id=case['subject'],
        block_id=block, task_type='remedial', title='보존 보충', scheduled_date=TODAY, estimated_minutes=15,
        status='planned', created_at=NOW))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=remedial, unit_id=case['unit'],
        start_page=1, end_page=6, sort_order=1))
    preserved = dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == remedial)).mappings().one())
    records = [dict(row) for row in conn.execute(select(t.study_records)).mappings()]
    conn.commit()
    response = draft(api_client, case, draftSubjectIds=case['added'])
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['summary']['totalPages'] == 26
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id == case['added'])) == 'draft'
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == task['id'])) == 'partial'
    conn.commit()
    applied = approve(api_client, case, value['previewId'])
    assert applied.status_code == 200, applied.text
    assert conn.scalar(select(t.plans.c.revision)) == 2
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == remedial)).mappings().one()) == preserved
    assert [dict(row) for row in conn.execute(select(t.study_records)).mappings()] == records
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == task['id'])) == 'carried_over'
    pending = conn.execute(select(t.study_task_ranges).join(t.study_tasks, t.study_tasks.c.id == t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.status == 'planned', t.study_tasks.c.task_type == 'study',
               t.study_tasks.c.subject_id == case['subject'])).mappings().all()
    assert sorted(page for row in pending for page in range(row['start_page'], row['end_page'] + 1)) == list(range(6, 21))
    conn.commit()
    assert approve(api_client, case, value['previewId']).json() == applied.json()


def test_started_placement_and_pending_session_are_unchanged(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    task = case['old_task']
    session = str(uuid4())
    conn.execute(t.study_sessions.insert().values(id=session, user_id=case['user'], task_id=task['id'],
        status='ended', started_at=NOW, ended_at=NOW + timedelta(minutes=10), measured_seconds=600,
        study_date=TODAY, created_at=NOW))
    conn.commit()
    response = draft(api_client, case, draftSubjectIds=case['added'])
    assert response.status_code == 200, response.text
    assert approve(api_client, case, response.json()['data']['previewId']).status_code == 200
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == task['id'])).mappings().one()) == task
    assert conn.scalar(select(t.study_sessions.c.id)) == session
    study = conn.execute(select(t.study_tasks).where(t.study_tasks.c.subject_id == case['subject'])).all()
    assert len(study) == 1


def test_overdue_supplement_is_explicit_and_never_omitted(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == case['old_task']['id']).values(
        task_type='remedial', scheduled_date=TODAY - timedelta(days=1)))
    conn.commit()
    response = draft(api_client, case, draftSubjectIds=case['added'])
    assert response.status_code == 409 and response.json()['error']['code'] == 'OVERDUE_OBLIGATIONS', response.text
    assert conn.scalar(select(t.plans.c.revision)) == 1
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id == case['added'])) == 'draft'


def test_new_record_after_preview_is_stale(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    response = draft(api_client, case, draftSubjectIds=case['added'])
    assert response.status_code == 200, response.text
    preview = response.json()['data']['previewId']
    conn.execute(t.study_records.insert().values(id=str(uuid4()), task_id=case['old_task']['id'], result='none',
        study_date=TODAY, recorded_at=NOW, updated_at=NOW))
    conn.commit()
    response = approve(api_client, case, preview)
    assert response.status_code == 409 and response.json()['error']['code'] == 'PREVIEW_STALE', response.text
