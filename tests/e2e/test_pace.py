from datetime import datetime, time, timedelta, timezone
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_pace_samples import sample, save_placements


@pytest.fixture
def pace_case(progress_world):
    w=progress_world;unit=w.unit(1,10)
    evidence=[sample(w,unit,day=w.day-timedelta(days=offset)) for offset in range(3)]
    future=w.task((unit,1,10),day=w.day+timedelta(days=1))
    current=w.task((unit,1,10))
    w.conn.commit()
    return w,unit,evidence,future,current


def alert(client,w):
    w.conn.commit()
    response=client.get('/api/today',headers={'X-Anonymous-Id':w.user})
    assert response.status_code==200,response.text
    return response.json()['data']['paceAlert']


def decide(client,w,value,apply=True,**kwargs):
    return client.post('/api/plan/pace',headers={'X-Anonymous-Id':w.user},json={
        'subjectId':w.subject['id'],'calibrationId':value['calibrationId'],'apply':apply,**kwargs})


def test_proposal_replays_then_atomically_replaces_only_unstarted_future(api_client,pace_case):
    w,unit,evidence,future,current=pace_case
    before_current=dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==current)).mappings().one())
    old_future=dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).mappings().one())
    before_records=w.conn.execute(select(t.study_records)).all()
    value=alert(api_client,w)
    assert value and value['days']==3 and value['sessions']==3 and value['ratio']==1.2
    assert value['perPage']==3 and value['expectedPerPage']==2.5
    assert alert(api_client,w)==value
    assert [row['taskId'] for row in value['rows']]==[future]
    assert value['rows'][0]['after']==30 and value['rows'][0]['pages']==10
    assert len(value['rows'][0]['ranges'])==1
    assert w.conn.scalar(select(t.subjects.c.minutes_per_page).where(t.subjects.c.id==w.subject['id']))==Fraction(5,2)
    w.conn.commit()
    response=decide(api_client,w,value)
    assert response.status_code==200,response.text
    assert response.json()['data']=={'applied':True,'overDays':[],'replan':False}
    assert decide(api_client,w,value).json()==response.json()
    assert w.conn.scalar(select(t.subjects.c.minutes_per_page).where(t.subjects.c.id==w.subject['id']))==3
    new=w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==future)).mappings().one()
    assert new['block_id']==old_future['block_id'] and new['estimated_minutes']==30
    assert w.conn.scalar(select(t.study_task_ranges.c.planned_minutes).where(t.study_task_ranges.c.task_id==new['id']))==30
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==future))=='carried_over'
    assert dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==current)).mappings().one())==before_current
    assert w.conn.execute(select(t.study_records)).all()==before_records
    assert alert(api_client,w) is None


def test_dismiss_has_no_speed_plan_or_task_write_and_replays_next_day(api_client,pace_case):
    w,_,_,_,_=pace_case
    value=alert(api_client,w)
    before_tasks=w.conn.execute(select(t.study_tasks)).all()
    before_plan=w.conn.execute(select(t.plans).where(t.plans.c.id==w.plan['id'])).one()
    w.conn.commit()
    response=decide(api_client,w,value,False)
    assert response.status_code==200,response.text
    assert response.json()['data']=={'applied':False,'overDays':[],'replan':False}
    assert w.conn.execute(select(t.study_tasks)).all()==before_tasks
    assert w.conn.execute(select(t.plans).where(t.plans.c.id==w.plan['id'])).one()==before_plan
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day+timedelta(days=1)));w.conn.commit()
    assert decide(api_client,w,value,False).json()==response.json()
    assert decide(api_client,w,value,True).status_code==409


@pytest.mark.parametrize('mutation',['record','baseline','date','plan_revision','session_start'])
def test_changed_basis_rejects_old_approval_without_partial_writes(api_client,pace_case,mutation):
    w,_,evidence,future,_=pace_case
    value=alert(api_client,w)
    if mutation=='record': w.conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==evidence[0]['record']).values(end_page=9))
    if mutation=='baseline': w.conn.execute(update(t.subjects).where(t.subjects.c.id==w.subject['id']).values(minutes_per_page=4))
    if mutation=='date': w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day+timedelta(days=1)))
    if mutation=='plan_revision': w.conn.execute(update(t.plans).where(t.plans.c.id==w.plan['id']).values(revision=99))
    if mutation=='session_start':
        w.conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=w.user,task_id=future,status='ended',
            started_at=w.now,ended_at=w.now,study_date=w.day,measured_seconds=0,created_at=w.now))
    before=w.conn.execute(select(t.study_tasks)).all();w.conn.commit()
    response=decide(api_client,w,value)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert w.conn.execute(select(t.study_tasks)).all()==before


def test_already_started_future_task_is_fixed(api_client,pace_case):
    w,_,_,future,_=pace_case
    w.conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=w.user,task_id=future,status='ended',
        started_at=w.now,ended_at=w.now,study_date=w.day,measured_seconds=0,created_at=w.now))
    before=dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).mappings().one())
    value=alert(api_client,w)
    assert value['rows']==[]
    assert decide(api_client,w,value).status_code==200
    assert dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).mappings().one())==before


def test_overflow_saves_p2_input_and_suppresses_p4(api_client,pace_case):
    w,_,_,future,_=pace_case
    start=datetime.combine(w.day+timedelta(days=1),time(13,45),timezone.utc) # Seoul22:45
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==future).values(starts_at=start,ends_at=start+timedelta(minutes=25)))
    value=alert(api_client,w)
    assert value['overDays'] and value['overDays'][0]['date']==(w.day+timedelta(days=1)).isoformat()
    response=decide(api_client,w,value)
    assert response.status_code==200,response.text
    assert response.json()['data']['replan']
    request=w.conn.execute(select(t.plan_change_requests).where(t.plan_change_requests.c.user_id==w.user,
        t.plan_change_requests.c.kind=='recovery',t.plan_change_requests.c.status=='ready')).mappings().one()
    assert request['recovery_context']['reason']=='pace'
    assert request['recovery_context']['calibrationId']==value['calibrationId']
    assert alert(api_client,w) is None


def test_foreign_calibration_and_invalid_commands_do_not_mutate(api_client,pace_case):
    w,_,_,_,_=pace_case;value=alert(api_client,w)
    other=api_client.post('/api/session').json()['data']['anonymousId']
    response=api_client.post('/api/plan/pace',headers={'X-Anonymous-Id':other},json={
        'subjectId':w.subject['id'],'calibrationId':value['calibrationId'],'apply':True})
    assert response.status_code==404
    assert decide(api_client,w,value,apply='true').status_code==422
    assert decide(api_client,w,value,unexpected=True).status_code==422
    assert w.conn.scalar(select(t.speed_calibrations.c.status).where(t.speed_calibrations.c.id==value['calibrationId']))=='pending'


def test_final_response_failure_rolls_back_speed_tasks_and_calibration(api_client,pace_case,monkeypatch):
    from app.repositories.proposals import ProposalRepository
    w,_,_,future,_=pace_case;value=alert(api_client,w)
    before_tasks=w.conn.execute(select(t.study_tasks)).all()
    before_plan=w.conn.execute(select(t.plans).where(t.plans.c.id==w.plan['id'])).one()
    original=ProposalRepository.mark_applied
    def fail(*args,**kwargs): raise RuntimeError('forced response persistence failure')
    monkeypatch.setattr(ProposalRepository,'mark_applied',fail)
    w.conn.commit()
    response=decide(api_client,w,value)
    assert response.status_code==500
    assert w.conn.execute(select(t.study_tasks)).all()==before_tasks
    assert w.conn.execute(select(t.plans).where(t.plans.c.id==w.plan['id'])).one()==before_plan
    assert w.conn.scalar(select(t.subjects.c.minutes_per_page).where(t.subjects.c.id==w.subject['id']))==Fraction(5,2)
    assert w.conn.scalar(select(t.speed_calibrations.c.status).where(t.speed_calibrations.c.id==value['calibrationId']))=='pending'
    w.conn.commit();monkeypatch.setattr(ProposalRepository,'mark_applied',original)
    assert decide(api_client,w,value).status_code==200


def test_pending_recovery_takes_priority_without_consuming_daily_pace_offer(api_client,pace_case):
    w,_,_,_,_=pace_case
    request=str(uuid4())
    w.conn.execute(t.plan_change_requests.insert().values(id=request,user_id=w.user,base_plan_id=w.plan['id'],
        base_plan_revision=1,kind='recovery',status='ready',input_payload={'schemaVersion':'1'},
        recovery_context={'subjects':[],'tasks':[]},created_at=w.now,updated_at=w.now))
    assert alert(api_client,w) is None
    assert w.conn.scalar(select(t.speed_calibrations.c.id).where(t.speed_calibrations.c.subject_id==w.subject['id'])) is None
    w.conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id==request).values(status='stale'));w.conn.commit()
    assert alert(api_client,w) is not None


def test_changed_sample_on_get_marks_stale_and_does_not_issue_second_daily_offer(api_client,pace_case):
    w,_,evidence,_,_=pace_case;value=alert(api_client,w)
    w.conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==evidence[0]['record']).values(end_page=9))
    assert alert(api_client,w) is None
    assert w.conn.scalar(select(t.speed_calibrations.c.status).where(t.speed_calibrations.c.id==value['calibrationId']))=='stale'
    w.conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==evidence[0]['record']).values(end_page=10))
    assert alert(api_client,w) is None
