"""Independent lifecycle oracles for settings and archival proposals."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select,update
from app.db import schema as t
from app.services.daily_totals import DailyTotalsService
from app.services.subject_archive_state import archived_dates
from tests.test_plan_draft import TODAY,NOW,plan_case
from tests.test_plan_addition_e2e import addition_case
from tests.test_subject_archive import preview as archive_preview,apply as archive_apply
from tests.test_subject_changes import preview as settings_preview,apply as settings_apply


def facts(conn):
    return tuple(conn.execute(select(table).order_by(table.c.id)).all()
        for table in (t.study_records,t.study_record_ranges,t.study_blocks,t.quiz_attempts,t.mastery_history))


def insert_focus(conn,case):
    identifier=str(uuid4())
    conn.execute(t.study_sessions.insert().values(id=identifier,user_id=case['user'],task_id=case['old_task']['id'],
        status='running',started_at=NOW,measured_seconds=0,study_date=TODAY,created_at=NOW))
    conn.commit()
    return identifier


def test_focus_start_after_preview_invalidates_without_mutating_subject(api_client,domain_connection,addition_case):
    operation = 'archive'
    conn,case=domain_connection,addition_case
    response=archive_preview(api_client,case['user'],case['subject']) if operation=='archive' else settings_preview(api_client,case,{'target':80})
    assert response.status_code==200,response.text
    pid=response.json()['data']['previewId']
    before=conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).one()
    tasks=conn.execute(select(t.study_tasks).order_by(t.study_tasks.c.id)).all();conn.commit()
    identifier=insert_focus(conn,case)
    response=archive_apply(api_client,case['user'],case['subject'],pid) if operation=='archive' else settings_apply(api_client,case,pid)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).one()==before
    assert conn.execute(select(t.study_tasks).order_by(t.study_tasks.c.id)).all()==tasks
    assert conn.scalar(select(t.study_sessions.c.status).where(t.study_sessions.c.id==identifier))=='running'
    assert conn.scalar(select(t.plans.c.revision))==1


def test_archive_preserves_frozen_past_and_original_demo_cutoff(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    original=case['old_task'];day=TODAY-timedelta(days=1)
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==original['id']).values(scheduled_date=day,status='partial',starts_at=None,ends_at=None))
    record=str(uuid4())
    conn.execute(t.study_records.insert().values(id=record,task_id=original['id'],result='partial',study_date=day,recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=7))
    conn.execute(t.daily_plan_totals.insert().values(user_id=case['user'],subject_id=case['subject'],study_date=day,
        plan_id=original['plan_id'],planned_new_pages=20,planned_total_pages=20,planned_unpaged_minutes=0,finalized_at=NOW,updated_at=NOW))
    preserved=facts(conn);frozen=conn.execute(select(t.daily_plan_totals)).all();conn.commit()
    candidate=archive_preview(api_client,case['user'],case['subject'])
    assert candidate.status_code==200,candidate.text
    pid=candidate.json()['data']['previewId']
    response=archive_apply(api_client,case['user'],case['subject'],pid)
    assert response.status_code==200,response.text
    subject=dict(conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).mappings().one())
    user=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    assert archived_dates(conn,user,[subject])=={case['subject']:TODAY}
    assert facts(conn)==preserved and conn.execute(select(t.daily_plan_totals)).all()==frozen
    assert (DailyTotalsService(conn,user).view(day)['plan'],DailyTotalsService(conn,user).view(day)['done'])==(20,7)
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=TODAY+timedelta(days=4)));conn.commit()
    user['demo_date']=TODAY+timedelta(days=4)
    assert archived_dates(conn,user,[subject])=={case['subject']:TODAY}
    conn.commit()
    assert archive_apply(api_client,case['user'],case['subject'],pid).json()==response.json()


def test_archive_followup_and_parent_replay_survive_date_change(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    candidate=archive_preview(api_client,case['user'],case['subject'])
    assert candidate.status_code==200,candidate.text
    pid=candidate.json()['data']['previewId']
    archived=archive_apply(api_client,case['user'],case['subject'],pid)
    assert archived.status_code==200,archived.text
    child=archived.json()['data']['previewId']
    assert child is not None and child!=pid
    headers={'X-Anonymous-Id':case['user']}
    applied=api_client.post('/api/plan/empty-time',headers=headers,json={'option':'rest','previewId':child})
    assert applied.status_code==200,applied.text
    revision=conn.scalar(select(t.plans.c.revision));facts_before=facts(conn)
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=TODAY+timedelta(days=1)));conn.commit()
    assert archive_apply(api_client,case['user'],case['subject'],pid).json()==archived.json()
    assert api_client.post('/api/plan/empty-time',headers=headers,json={'option':'rest','previewId':child}).json()==applied.json()
    other=api_client.post('/api/plan/empty-time',headers=headers,json={'option':'pull','previewId':child})
    assert other.status_code==409,other.text
    assert conn.scalar(select(t.plans.c.revision))==revision and facts(conn)==facts_before


def test_unstarted_remedial_stays_durable_but_does_not_return_on_next_demo_day(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    row=case['old_task'];tomorrow=TODAY+timedelta(days=1)
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==row['id']).values(task_type='remedial',scheduled_date=tomorrow))
    preserved=conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==row['id'])).one();conn.commit()
    candidate=archive_preview(api_client,case['user'],case['subject'])
    assert candidate.status_code==200,candidate.text
    applied=archive_apply(api_client,case['user'],case['subject'],candidate.json()['data']['previewId'])
    assert applied.status_code==200,applied.text
    assert conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==row['id'])).one()==preserved
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=tomorrow));conn.commit()
    today=api_client.get('/api/today',headers={'X-Anonymous-Id':case['user']})
    assert today.status_code==200,today.text
    assert today.json()['data']['tasks']==[]
    assert conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==row['id'])).one()==preserved
