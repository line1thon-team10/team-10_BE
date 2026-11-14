"""Remaining-plan generation never silently approves or discards historical facts."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update, event
from app.db import schema as t
from tests.test_plan_draft import NOW,TODAY,plan_case
from tests.test_plan_addition_e2e import addition_case


def state(conn):
    return {table.name:conn.execute(select(table).order_by(*table.primary_key.columns)).all()
        for table in (t.plans,t.subjects,t.units,t.study_tasks,t.study_task_ranges,t.study_records,t.study_record_ranges,t.study_sessions,t.learning_passes)}


def call(client,case,method,path,**kwargs):
    return getattr(client,method)(path,headers={'X-Anonymous-Id':case['user']},**kwargs)


def test_rebuild_is_candidate_then_atomic_replayable_approval(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    before=state(conn);conn.commit()
    response=call(api_client,case,'post','/api/plan/rebuild',json={})
    assert response.status_code==200,response.text
    data=response.json()['data'];assert data['ready'] and data['question'] is None
    assert state(conn)==before;conn.commit()
    compare=call(api_client,case,'get','/api/plan/resplit',params={'previewId':data['previewId']})
    assert compare.status_code==200,compare.text
    value=compare.json()['data']
    assert all(type(row['before']) is int and type(row['after']) is int for row in value['days'])
    assert sum(row['before'] for row in value['days'])==sum(row['after'] for row in value['days'])==case['old_task']['estimated_minutes']
    assert value['stats'][0]['before']==value['stats'][0]['after']==case['old_task']['estimated_minutes']
    repeat=call(api_client,case,'post','/api/plan/rebuild',json={})
    assert repeat.json()==response.json()
    applied=call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':data['previewId']})
    assert applied.status_code==200 and applied.json()['data']=={'applied':True},applied.text
    assert conn.scalar(select(t.plans.c.revision))==2
    after=state(conn);conn.commit()
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':data['previewId']}).json()==applied.json()
    assert state(conn)==after


def test_cancel_and_foreign_candidate_leave_every_domain_fact_unchanged(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    before=state(conn);conn.commit()
    identifier=call(api_client,case,'post','/api/plan/rebuild',json={}).json()['data']['previewId']
    foreign={**case,'user':case['other_user']}
    for method,path,kwargs in [('get','/api/plan/resplit',{'params':{'previewId':identifier}}),
                              ('post','/api/plan/resplit/apply',{'json':{'previewId':identifier}}),
                              ('delete','/api/plan/resplit',{'params':{'previewId':identifier}})]:
        assert call(api_client,foreign,method,path,**kwargs).status_code==404
    assert call(api_client,case,'delete','/api/plan/resplit',params={'previewId':identifier}).json()['data']=={'cancelled':True}
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier}).status_code==409
    assert state(conn)==before


def test_partial_learning_and_completed_session_remain_history(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    task=case['old_task'];record,session=str(uuid4()),str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(status='partial',
        scheduled_date=TODAY-timedelta(days=1),starts_at=None,ends_at=None))
    conn.execute(t.study_sessions.insert().values(id=session,user_id=case['user'],task_id=task['id'],status='ended',
        started_at=NOW-timedelta(days=1),ended_at=NOW-timedelta(days=1)+timedelta(minutes=15),
        measured_seconds=900,study_date=TODAY-timedelta(days=1),created_at=NOW))
    conn.execute(t.study_records.insert().values(id=record,task_id=task['id'],session_id=session,result='partial',
        study_date=TODAY-timedelta(days=1),recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=5))
    before=state(conn);conn.commit()
    response=call(api_client,case,'post','/api/plan/rebuild',json={});assert response.status_code==200,response.text
    response=call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':response.json()['data']['previewId']})
    assert response.status_code==200,response.text
    after=state(conn)
    for table in ['study_records','study_record_ranges','study_sessions','learning_passes']:
        assert after[table]==before[table]
    rows=conn.execute(select(t.study_task_ranges).join(t.study_tasks,t.study_tasks.c.id==t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.status=='planned',t.study_tasks.c.subject_id==case['subject'])).mappings()
    assert sorted(page for row in rows for page in range(row['start_page'],row['end_page']+1))==list(range(6,21))


def test_rebuild_requires_existing_plan(api_client,plan_case):
    response=call(api_client,plan_case,'post','/api/plan/rebuild',json={})
    assert response.status_code==400 and response.json()['error']['code']=='NO_PLAN'


def test_failed_apply_rolls_back_and_can_retry(api_client,domain_connection,addition_case):
    from app.db import session
    conn,case=domain_connection,addition_case
    identifier=call(api_client,case,'post','/api/plan/rebuild',json={}).json()['data']['previewId']
    before=state(conn);conn.commit()
    def fail(connection,cursor,sql,*args):
        if sql.startswith('INSERT INTO study_tasks '):
            raise RuntimeError('write fault')
    event.listen(session.engine,'before_cursor_execute',fail)
    try:
        response=call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier})
    finally:
        event.remove(session.engine,'before_cursor_execute',fail)
    assert response.status_code==500,response.text
    assert state(conn)==before;conn.commit()
    assert call(api_client,case,'post','/api/plan/resplit/apply',json={'previewId':identifier}).status_code==200
