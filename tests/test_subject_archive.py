"""Removing plan membership never removes its underlying learning facts."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select,update
from app.db import schema as t
from tests.test_plan_draft import TODAY,NOW,plan_case
from tests.test_plan_addition_e2e import addition_case
from tests.test_onboarding_events import events_user,add_subject


def preview(client,user,sid):
    return client.delete(f'/api/subjects/{sid}/preview',headers={'X-Anonymous-Id':user})


def apply(client,user,sid,pid):
    return client.delete(f'/api/subjects/{sid}',headers={'X-Anonymous-Id':user},params={'previewId':pid})


def test_draft_without_plan_archives_and_replays_empty_time_response(api_client,domain_connection,events_user):
    conn,user=domain_connection,events_user
    sid=add_subject(conn,user['id']);conn.commit()
    response=preview(api_client,user['id'],sid)
    assert response.status_code==200,response.text
    pid=response.json()['data']['previewId']
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==sid))=='draft'
    conn.commit()
    response=apply(api_client,user['id'],sid,pid)
    assert response.status_code==200,response.text
    assert response.json()['data']=={'emptyTime':None,'slots':[],'options':[],'previewId':None}
    assert conn.scalar(select(t.subjects.c.archive_reason).where(t.subjects.c.id==sid))=='user_removed'
    assert conn.scalar(select(t.plans.c.id)) is None
    conn.commit()
    assert apply(api_client,user['id'],sid,pid).json()==response.json()


@pytest.mark.parametrize('status,recorded', [('running',False),('paused',False),('ended',False),('auto_stopped',False)])
def test_open_or_unrecorded_session_blocks_archive(api_client,domain_connection,addition_case,status,recorded):
    conn,case=domain_connection,addition_case
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=case['old_task']['id'],
        status=status,started_at=NOW,ended_at=NOW if status in ('ended','auto_stopped') else None,
        measured_seconds=600,study_date=TODAY,created_at=NOW))
    conn.commit()
    response=preview(api_client,case['user'],case['subject'])
    assert response.status_code==409 and response.json()['error']['code']=='ACTIVE_SESSION',response.text
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==case['subject']))=='active'


def test_record_and_remedial_facts_survive_archive_without_completion(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    task=case['old_task']
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(status='partial',task_type='remedial'))
    conn.execute(t.study_records.insert().values(id=str(uuid4()),task_id=task['id'],result='partial',study_date=TODAY,
        recorded_at=NOW,updated_at=NOW))
    record=conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id==task['id']))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=5))
    facts=[conn.execute(select(table)).all() for table in (t.study_tasks,t.study_task_ranges,t.study_records,t.study_record_ranges,t.study_blocks)]
    conn.commit()
    candidate=preview(api_client,case['user'],case['subject'])
    assert candidate.status_code==200,candidate.text
    response=apply(api_client,case['user'],case['subject'],candidate.json()['data']['previewId'])
    assert response.status_code==200,response.text
    assert response.json()['data']['emptyTime'] is None
    assert [conn.execute(select(table)).all() for table in (t.study_tasks,t.study_task_ranges,t.study_records,t.study_record_ranges,t.study_blocks)]==facts
    conn.commit()
    today=api_client.get('/api/today',headers={'X-Anonymous-Id':case['user']})
    assert today.status_code==200,today.text
    assert today.json()['data']['tasks']==[]


def test_foreign_subject_and_foreign_preview_are_rejected(api_client,addition_case):
    case=addition_case
    assert preview(api_client,case['user'],case['foreign']).status_code==404
    candidate=preview(api_client,case['user'],case['added'])
    assert candidate.status_code==200,candidate.text
    response=apply(api_client,case['other_user'],case['added'],candidate.json()['data']['previewId'])
    assert response.status_code==404,response.text
    response=apply(api_client,case['user'],case['subject'],candidate.json()['data']['previewId'])
    assert response.status_code==404,response.text


def test_new_record_after_archive_preview_invalidates_candidate(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    candidate=preview(api_client,case['user'],case['subject'])
    assert candidate.status_code==200,candidate.text
    conn.execute(t.study_records.insert().values(id=str(uuid4()),task_id=case['old_task']['id'],result='none',
        study_date=TODAY,recorded_at=NOW,updated_at=NOW));conn.commit()
    response=apply(api_client,case['user'],case['subject'],candidate.json()['data']['previewId'])
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==case['subject']))=='active'


def test_released_time_offer_uses_new_revision_and_parent_response_replays(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    candidate=preview(api_client,case['user'],case['subject'])
    assert candidate.status_code==200,candidate.text
    parent=candidate.json()['data']['previewId']
    archived=apply(api_client,case['user'],case['subject'],parent)
    assert archived.status_code==200,archived.text
    response=archived.json()['data']
    assert response['emptyTime'] is not None and response['slots']
    assert {option['key'] for option in response['options']}=={'rest','pull','advance'}
    assert response['previewId']!=parent
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==case['old_task']['id']))=='excluded'
    assert conn.scalar(select(t.plans.c.revision))==2
    conn.commit()
    rest=api_client.post('/api/plan/empty-time',headers={'X-Anonymous-Id':case['user']},
        json={'previewId':response['previewId'],'option':'rest'})
    assert rest.status_code==200,rest.text
    assert apply(api_client,case['user'],case['subject'],parent).json()==archived.json()


def test_archive_preserves_unstarted_remedial_rows_but_releases_plan_membership(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['old_task']['id']).values(task_type='remedial'))
    before=conn.execute(select(t.study_tasks)).all();conn.commit()
    candidate=preview(api_client,case['user'],case['subject'])
    response=apply(api_client,case['user'],case['subject'],candidate.json()['data']['previewId'])
    assert response.status_code==200,response.text
    assert response.json()['data']['emptyTime'] is not None
    assert conn.execute(select(t.study_tasks)).all()==before
    conn.commit()
    assert api_client.get('/api/today',headers={'X-Anonymous-Id':case['user']}).json()['data']['tasks']==[]


def test_offer_failure_rolls_back_archive_and_plan_revision(api_client,domain_connection,addition_case,monkeypatch):
    from app.services.empty_time import EmptyTimeService
    from app.core.responses import ApiError
    conn,case=domain_connection,addition_case
    candidate=preview(api_client,case['user'],case['subject'])
    assert candidate.status_code==200,candidate.text
    original=EmptyTimeService.offer
    def fail(*args):
        raise ApiError('PLAN_INFEASIBLE','injected rollback',409)
    monkeypatch.setattr(EmptyTimeService,'offer',fail)
    pid=candidate.json()['data']['previewId']
    response=apply(api_client,case['user'],case['subject'],pid)
    assert response.status_code==409,response.text
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==case['subject']))=='active'
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==case['old_task']['id']))=='planned'
    assert conn.scalar(select(t.plans.c.revision))==1
    conn.commit()
    monkeypatch.setattr(EmptyTimeService,'offer',original)
    assert apply(api_client,case['user'],case['subject'],pid).status_code==200
