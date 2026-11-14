"""Subject settings through HTTP and migrated transactional databases."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_draft import TODAY, NOW, plan_case
from tests.test_plan_addition_e2e import addition_case


def preview(client, case, body, subject=None):
    return client.patch(f"/api/subjects/{subject or case['subject']}/preview",
        headers={'X-Anonymous-Id':case['user']},json=body)


def apply(client, case, preview_id, subject=None):
    return client.patch(f"/api/subjects/{subject or case['subject']}",
        headers={'X-Anonymous-Id':case['user']},json={'previewId':preview_id})


@pytest.mark.parametrize('body,status', [({},400),({'goal':'   '},400),({'goal':None},422),({'target':None},422),
    ({'examDate':None},422),({'target':True},422),({'target':'90'},422),({'target':-1},422),({'target':101},422),
    ({'examDate':'2026-11-16'},422),({'examDate':'2026-02-30'},422),({'examDate':'2026-1-01'},422),
    ({'goal':'A\x00'},422),({'target':80,'unknown':1},422),({'goal':'A+','target':90},400)])
def test_invalid_or_unchanged_setting_does_not_write(api_client, domain_connection, addition_case, body, status):
    case,conn=addition_case,domain_connection
    before=conn.execute(select(t.subjects).order_by(t.subjects.c.id)).all()
    tasks=conn.execute(select(t.study_tasks).order_by(t.study_tasks.c.id)).all()
    conn.commit()
    response=preview(api_client,case,body)
    assert response.status_code==status,response.text
    assert conn.execute(select(t.subjects).order_by(t.subjects.c.id)).all()==before
    assert conn.execute(select(t.study_tasks).order_by(t.study_tasks.c.id)).all()==tasks
    assert conn.scalar(select(t.plans.c.revision))==1


@pytest.mark.parametrize('target', ['foreign','added','missing'])
def test_other_or_unapproved_subject_cannot_be_changed(api_client,addition_case,target):
    case=addition_case
    identifier=str(uuid4()) if target=='missing' else case[target]
    response=preview(api_client,case,{'target':80},identifier)
    assert response.status_code==(409 if target=='added' else 404),response.text


def test_goal_preview_preserves_today_and_applies_with_response_replay(api_client,domain_connection,addition_case):
    case,conn=addition_case,domain_connection
    old=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==case['old_task']['id'])).mappings().one())
    shift=TODAY-old['scheduled_date']
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==old['id']).values(
        scheduled_date=TODAY, starts_at=old['starts_at']+shift, ends_at=old['ends_at']+shift))
    old=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==old['id'])).mappings().one())
    subject=dict(conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).mappings().one())
    conn.commit()
    response=preview(api_client,case,{'goal':'B+','target':80})
    assert response.status_code==200,response.text
    candidate=response.json()['data']
    assert candidate['note'] and candidate['subjectName']==subject['name']
    assert dict(conn.execute(select(t.subjects).where(t.subjects.c.id==case['subject'])).mappings().one())==subject
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==old['id'])).mappings().one())==old
    conn.commit()
    assert preview(api_client,case,{'goal':'B+','target':80}).json()==response.json()
    applied=apply(api_client,case,candidate['previewId'])
    assert applied.status_code==200,applied.text
    assert applied.json()['data']['goal']=='B+' and applied.json()['data']['target']==80
    assert conn.scalar(select(t.subjects.c.raw_goal).where(t.subjects.c.id==case['subject']))=='B+'
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==old['id'])).mappings().one())==old
    assert conn.scalar(select(t.plans.c.revision))==2
    conn.commit()
    assert apply(api_client,case,candidate['previewId']).json()==applied.json()
    assert conn.scalar(select(t.plans.c.revision))==2


def test_date_and_goal_change_reallocates_only_future_subject_work(api_client,domain_connection,addition_case):
    case,conn=addition_case,domain_connection
    old=case['old_task']; tomorrow=TODAY+timedelta(days=1)
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==old['id']).values(
        scheduled_date=tomorrow, starts_at=old['starts_at']+timedelta(days=1),ends_at=old['ends_at']+timedelta(days=1)))
    conn.commit()
    response=preview(api_client,case,{'examDate':'2026-11-18','target':75})
    assert response.status_code==200,response.text
    candidate=response.json()['data']
    assert all(day['date']=='2026-11-17' for day in candidate['days'])
    assert apply(api_client,case,candidate['previewId']).status_code==200
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id==case['subject'])).isoformat()=='2026-11-18'
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==old['id']))=='carried_over'
    fresh=conn.execute(select(t.study_tasks).where(t.study_tasks.c.status=='planned')).mappings().all()
    assert fresh and all(row['scheduled_date']==tomorrow for row in fresh)


def test_infeasible_earlier_exam_never_changes_current_plan(api_client,domain_connection,addition_case):
    case,conn=addition_case,domain_connection
    conn.execute(update(t.materials).where(t.materials.c.id==case['material']).values(page_count=1000))
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(end_page=1000))
    conn.commit()
    response=preview(api_client,case,{'examDate':'2026-11-17'})
    assert response.status_code==409 and response.json()['error']['code']=='PLAN_INFEASIBLE',response.text
    assert conn.scalar(select(t.plans.c.revision))==1
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id==case['subject']))==TODAY+timedelta(days=3)


def test_record_after_preview_or_other_candidate_apply_is_stale(api_client,domain_connection,addition_case):
    case,conn=addition_case,domain_connection
    first=preview(api_client,case,{'target':80});second=preview(api_client,case,{'target':70})
    assert first.status_code==second.status_code==200
    assert apply(api_client,case,first.json()['data']['previewId']).status_code==200
    stale=apply(api_client,case,second.json()['data']['previewId'])
    assert stale.status_code==409 and stale.json()['error']['code']=='PREVIEW_STALE'
    third=preview(api_client,case,{'target':60})
    assert third.status_code==200,third.text
    conn.execute(t.study_records.insert().values(id=str(uuid4()),task_id=case['old_task']['id'],result='none',
        study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    conn.commit()
    stale=apply(api_client,case,third.json()['data']['previewId'])
    assert stale.status_code==409 and stale.json()['error']['code']=='PREVIEW_STALE'


def test_applied_goal_weight_is_effective_tomorrow_even_after_second_same_day_change(api_client,domain_connection,addition_case):
    from app.services.plan_inputs import existing_allocation_input
    case,conn=addition_case,domain_connection
    for target in (80,70):
        response=preview(api_client,case,{'target':target})
        assert response.status_code==200,response.text
        result=apply(api_client,case,response.json()['data']['previewId'])
        assert result.status_code==200,result.text
    request,_,_=existing_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',
        subject_ids=(),affected_subject_ids=(case['subject'],))
    subject=next(s for s in request.subjects if s.id==case['subject'])
    assert subject.target_on(TODAY)==90
    assert subject.target_on(TODAY+timedelta(days=1))==70
    next_day,_,_=existing_allocation_input(conn,case['user'],today=TODAY+timedelta(days=1),timezone_name='Asia/Seoul',
        subject_ids=(),affected_subject_ids=(case['subject'],))
    subject=next(s for s in next_day.subjects if s.id==case['subject'])
    assert subject.target_on(TODAY+timedelta(days=1))==70


def test_subject_update_and_allocation_roll_back_together(api_client,domain_connection,addition_case,monkeypatch):
    from app.services import subject_changes
    from app.core.responses import ApiError
    case,conn=addition_case,domain_connection
    response=preview(api_client,case,{'goal':'B+','target':80})
    assert response.status_code==200,response.text
    original=subject_changes.apply_placements
    def fail(*args):
        raise ApiError('PLAN_INFEASIBLE','injected atomicity failure',409)
    monkeypatch.setattr(subject_changes,'apply_placements',fail)
    pid=response.json()['data']['previewId']
    assert apply(api_client,case,pid).status_code==409
    assert conn.scalar(select(t.subjects.c.target_score).where(t.subjects.c.id==case['subject']))==90
    assert conn.scalar(select(t.plans.c.revision))==1
    conn.commit()
    monkeypatch.setattr(subject_changes,'apply_placements',original)
    assert apply(api_client,case,pid).status_code==200


def test_setting_candidate_cannot_apply_to_another_subject_or_owner(api_client,addition_case):
    case=addition_case
    response=preview(api_client,case,{'target':80})
    assert response.status_code==200,response.text
    pid=response.json()['data']['previewId']
    assert apply(api_client,case,pid,subject=case['added']).status_code==404
    assert apply(api_client,{**case,'user':case['other_user']},pid).status_code==404
    assert apply(api_client,case,pid).status_code==200
