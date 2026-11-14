"""HTTP and persisted preview/move facts, with external-free isolated databases."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_draft_e2e import draft, approve


@pytest.fixture
def today_case(api_client,domain_connection,plan_case):
    conn,case=domain_connection,plan_case
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['active']).values(status='archived'))
    conn.commit()
    assert approve(api_client,case,draft(api_client,case).json()['data']['previewId']).status_code==200
    task=dict(conn.execute(select(t.study_tasks)).mappings().one())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(
        scheduled_date=TODAY,starts_at=NOW-timedelta(hours=1),ends_at=NOW-timedelta(hours=1)+timedelta(minutes=task['estimated_minutes'])))
    task=dict(conn.execute(select(t.study_tasks)).mappings().one())
    conn.commit()
    return {**case,'task':task,'headers':{'X-Anonymous-Id':case['user']}}


def copy_task(conn,case,*,day=TODAY+timedelta(days=1),hour=8,minutes=50,kind='study',start=1,end=20):
    task,block=str(uuid4()),str(uuid4())
    original=case['task']
    conn.execute(t.study_blocks.insert().values(id=block,subject_id=case['subject'],unit_id=case['unit'],task_type=kind,created_at=NOW))
    conn.execute(t.study_tasks.insert().values(**{**original,'id':task,'block_id':block,'origin_task_id':None,'task_type':kind,
        'scheduled_date':day,'starts_at':NOW.replace(hour=hour-9 if hour>=9 else 23)+timedelta(days=(day-TODAY).days-(hour<9)),
        'ends_at':NOW.replace(hour=hour-9 if hour>=9 else 23)+timedelta(days=(day-TODAY).days-(hour<9),minutes=minutes),
        'estimated_minutes':minutes,'status':'planned'}))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task,unit_id=case['unit'],
        start_page=start,end_page=end,sort_order=1))
    return task


def preview(client,case,action='more30',**extra):
    return client.post('/api/plan/today/preview',headers=case['headers'],json={'action':action,**extra})


def apply(client,case,candidate,action='more30',**extra):
    return client.post('/api/plan/today',headers=case['headers'],json={'action':action,'previewId':candidate,**extra})


def test_more_moves_entire_next_task_and_preserves_original_block(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case,minutes=45)
    original=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).mappings().one())
    before=conn.execute(select(t.study_tasks)).all();conn.commit()
    response=preview(api_client,case)
    assert response.status_code==200,response.text
    candidate=response.json()['data']['previewId']
    assert conn.execute(select(t.study_tasks)).all()==before
    conn.commit()
    assert preview(api_client,case).json()==response.json()
    applied=apply(api_client,case,candidate)
    assert applied.status_code==200 and applied.json()['data']=={'applied':True},applied.text
    original_after=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).mappings().one())
    assert original_after=={**original,'status':'carried_over'}
    new=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==future)).mappings().one())
    assert new['block_id']==original['block_id'] and new['scheduled_date']==TODAY and new['estimated_minutes']==45
    conn.commit()
    assert apply(api_client,case,candidate).json()==applied.json()
    assert conn.scalar(select(t.plans.c.revision))==2


def test_postpone_is_first_next_subject_day_without_losing_future_work(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    tomorrow=copy_task(conn,case,minutes=30)
    conn.commit()
    response=preview(api_client,case,'postpone',taskId=case['task']['id'])
    assert response.status_code==200,response.text
    candidate=response.json()['data']['previewId']
    assert apply(api_client,case,candidate,'postpone').status_code==200
    pending=list(conn.execute(select(t.study_tasks).where(t.study_tasks.c.status=='planned').order_by(t.study_tasks.c.starts_at)).mappings())
    assert [x['origin_task_id'] for x in pending]==[case['task']['id'],tomorrow]
    assert all(x['scheduled_date']==TODAY+timedelta(days=1) for x in pending)
    assert pending[0]['ends_at']<=pending[1]['starts_at']


@pytest.mark.parametrize('body',[{'action':'postpone'},{'action':'rest','taskId':str(uuid4())},
    {'action':'more30','taskId':str(uuid4())},{'action':'unknown'},{'action':'postpone','taskId':'x'}])
def test_invalid_command_has_no_preview(api_client,domain_connection,today_case,body):
    before=domain_connection.scalar(select(t.plans.c.revision));domain_connection.commit()
    response=api_client.post('/api/plan/today/preview',headers=today_case['headers'],json=body)
    assert response.status_code==422
    assert domain_connection.scalar(select(t.plans.c.revision))==before


def test_unknown_task_and_missing_future_have_specific_errors(api_client,today_case):
    assert preview(api_client,today_case).json()['error']['code']=='NO_TASK'
    foreign=preview(api_client,today_case,'postpone',taskId=str(uuid4()))
    assert foreign.status_code==404 and foreign.json()['error']['code']=='NO_TASK'
    assert preview(api_client,today_case,'postpone',taskId=today_case['task']['id']).json()['error']['code']=='NO_TASK'


def test_started_future_task_is_not_a_more_candidate(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case)
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=future,status='ended',
        started_at=NOW,ended_at=NOW+timedelta(minutes=5),measured_seconds=300,study_date=TODAY,created_at=NOW))
    conn.commit()
    response=preview(api_client,case)
    assert response.status_code==400 and response.json()['error']['code']=='NO_TASK'


def test_full_day_rejects_more_without_inflating_original_minutes(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case)
    conn.execute(t.calendar_events.insert().values(id=str(uuid4()),user_id=case['user'],event_type='job',title='busy',
        starts_at=NOW-timedelta(hours=1),ends_at=NOW+timedelta(hours=14),created_at=NOW,updated_at=NOW))
    conn.commit()
    response=preview(api_client,case)
    assert response.status_code==409 and response.json()['error']['code']=='PLAN_INFEASIBLE'
    assert conn.scalar(select(t.study_tasks.c.estimated_minutes).where(t.study_tasks.c.id==future))==50


def test_apply_checks_action_owner_and_changed_input(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case);conn.commit()
    candidate=preview(api_client,case).json()['data']['previewId']
    assert apply(api_client,case,candidate,'postpone').status_code==404
    assert apply(api_client,{**case,'headers':{'X-Anonymous-Id':case['other_user']}},candidate).status_code==404
    assert apply(api_client,case,candidate,taskId=future).status_code==422
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==future).values(title='changed'));conn.commit()
    response=apply(api_client,case,candidate)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE'
    assert conn.scalar(select(t.plans.c.revision))==1


def test_partial_postpone_keeps_recorded_pages_and_block_history(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    task=case['task']['id'];copy_task(conn,case,minutes=30)
    record=str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(status='partial'))
    conn.execute(t.study_records.insert().values(id=record,task_id=task,result='partial',study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=5))
    before=(conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all());conn.commit()
    response=preview(api_client,case,'postpone',taskId=task)
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId'],'postpone').status_code==200
    new=conn.scalar(select(t.study_tasks.c.id).where(t.study_tasks.c.origin_task_id==task))
    ranges=list(conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==new)).mappings())
    assert [(x['start_page'],x['end_page']) for x in ranges]==[(6,20)]
    assert (conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all())==before


def test_day_change_invalidates_unapplied_preview_but_not_applied_replay(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    copy_task(conn,case);conn.commit()
    candidate=preview(api_client,case).json()['data']['previewId']
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=TODAY+timedelta(days=1)));conn.commit()
    response=apply(api_client,case,candidate)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE'


def test_applied_reply_survives_next_day_without_duplicate_task(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case);conn.commit()
    candidate=preview(api_client,case).json()['data']['previewId']
    applied=apply(api_client,case,candidate)
    assert applied.status_code==200
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=TODAY+timedelta(days=1)));conn.commit()
    assert apply(api_client,case,candidate).json()==applied.json()
    assert len(conn.execute(select(t.study_tasks.c.id).where(t.study_tasks.c.origin_task_id==future)).all())==1


def test_mid_apply_failure_rolls_back_original_status_and_new_placement(api_client,domain_connection,today_case,monkeypatch):
    from app.services import plan_today
    conn,case=domain_connection,today_case
    future=copy_task(conn,case);conn.commit()
    candidate=preview(api_client,case).json()['data']['previewId']
    original=plan_today._instant
    monkeypatch.setattr(plan_today,'_instant',lambda *_: (_ for _ in ()).throw(RuntimeError('write fault')))
    failed=apply(api_client,case,candidate)
    assert failed.status_code==500
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==future))=='planned'
    assert not conn.execute(select(t.study_tasks.c.id).where(t.study_tasks.c.origin_task_id==future)).all()
    assert conn.scalar(select(t.plans.c.revision))==1
    conn.commit()
    monkeypatch.setattr(plan_today,'_instant',original)
    assert apply(api_client,case,candidate).status_code==200


def test_started_postpone_target_and_malformed_preview_do_not_move(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    copy_task(conn,case)
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=case['task']['id'],
        status='running',started_at=NOW,study_date=TODAY,created_at=NOW))
    conn.commit()
    response=preview(api_client,case,'postpone',taskId=case['task']['id'])
    assert response.status_code==400 and response.json()['error']['code']=='NO_TASK'
    assert apply(api_client,case,'bad-id').status_code==422
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==case['task']['id']))=='planned'
