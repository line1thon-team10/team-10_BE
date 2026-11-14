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


def insert_focus(conn,case):
    identifier=str(uuid4())
    conn.execute(t.study_sessions.insert().values(id=identifier,user_id=case['user'],task_id=case['old_task']['id'],
        status='running',started_at=NOW,measured_seconds=0,study_date=TODAY,created_at=NOW))
    conn.commit()
    return identifier


def test_focus_start_after_preview_invalidates_without_mutating_subject(api_client,domain_connection,addition_case):
    operation = 'settings'
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


def test_settings_competing_tabs_and_historical_response_replay(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    first=settings_preview(api_client,case,{'target':80})
    second=settings_preview(api_client,case,{'target':70})
    assert first.status_code==second.status_code==200,(first.text,second.text)
    a,b=first.json()['data']['previewId'],second.json()['data']['previewId']
    first_result=settings_apply(api_client,case,a)
    assert first_result.status_code==200,first_result.text
    stale=settings_apply(api_client,case,b)
    assert stale.status_code==409 and stale.json()['error']['code']=='PREVIEW_STALE',stale.text
    fresh=settings_preview(api_client,case,{'target':70})
    assert fresh.status_code==200,fresh.text
    updated=settings_apply(api_client,case,fresh.json()['data']['previewId'])
    assert updated.status_code==200,updated.text
    assert settings_apply(api_client,case,a).json()==first_result.json()
    assert conn.scalar(select(t.subjects.c.target_score).where(t.subjects.c.id==case['subject']))==70
    assert conn.scalar(select(t.plans.c.revision))==3


def test_preview_cannot_cross_operation_target_or_owner(api_client,addition_case):
    case=addition_case
    archive=archive_preview(api_client,case['user'],case['subject'])
    settings=settings_preview(api_client,case,{'target':80})
    assert archive.status_code==settings.status_code==200
    archived_id=archive.json()['data']['previewId'];settings_id=settings.json()['data']['previewId']
    assert settings_apply(api_client,case,archived_id).status_code==404
    assert archive_apply(api_client,case['user'],case['subject'],settings_id).status_code==404
    assert settings_apply(api_client,{**case,'user':case['other_user']},settings_id).status_code==404
    assert archive_apply(api_client,case['other_user'],case['subject'],archived_id).status_code==404
    assert settings_apply(api_client,case,settings_id,subject=case['added']).status_code==404
    assert archive_apply(api_client,case['user'],case['added'],archived_id).status_code==404


def test_earlier_exam_replans_only_unlearned_pages_and_goal_effective_day_is_respected(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    old=case['old_task'];record=str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==old['id']).values(
        scheduled_date=TODAY-timedelta(days=1),starts_at=None,ends_at=None,status='partial'))
    conn.execute(t.study_records.insert().values(id=record,task_id=old['id'],result='partial',
        study_date=TODAY-timedelta(days=1),recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=7))
    history=(conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all())
    original_subject=conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).one();conn.commit()
    tomorrow=(TODAY+timedelta(days=1)).isoformat()
    impossible=settings_preview(api_client,case,{'examDate':tomorrow,'target':70})
    assert impossible.status_code==409 and impossible.json()['error']['code']=='PLAN_INFEASIBLE',impossible.text
    assert conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).one()==original_subject
    conn.commit()
    preview=settings_preview(api_client,case,{'examDate':tomorrow})
    assert preview.status_code==200,preview.text
    result=settings_apply(api_client,case,preview.json()['data']['previewId'])
    assert result.status_code==200,result.text
    assert (conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all())==history
    rows=conn.execute(select(t.study_task_ranges).join(t.study_tasks,t.study_tasks.c.id==t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.subject_id==case['subject'],t.study_tasks.c.status=='planned',t.study_tasks.c.task_type=='study')).mappings().all()
    assert sorted(page for row in rows for page in range(row['start_page'],row['end_page']+1))==list(range(8,21))
    assert all(row==TODAY for row in conn.scalars(select(t.study_tasks.c.scheduled_date).where(
        t.study_tasks.c.subject_id==case['subject'],t.study_tasks.c.status=='planned')))


@pytest.mark.parametrize('target',[0,100])
def test_exact_target_bounds_remain_exact_on_applied_card(api_client,domain_connection,addition_case,target):
    conn,case=domain_connection,addition_case
    preview=settings_preview(api_client,case,{'target':target})
    assert preview.status_code==200,preview.text
    applied=settings_apply(api_client,case,preview.json()['data']['previewId'])
    assert applied.status_code==200,applied.text
    assert applied.json()['data']['target']==target
    assert conn.scalar(select(t.subjects.c.target_score).where(t.subjects.c.id==case['subject']))==target
