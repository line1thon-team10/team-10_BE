"""Independent P4 HTTP checks against persisted facts and approval outcomes."""
from datetime import datetime, time, timedelta, timezone
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.repositories.proposals import ProposalRepository
from tests.test_progress_contract import progress_world


def evidence(w, unit, day, *, seconds=1800, multiplier=(1,1), kind='study', snapshot=True):
    task=w.task((unit,1,10),done=[(unit,1,10)],status='completed',day=day,kind=kind)
    started=datetime.combine(day,time(8),timezone.utc)
    sid=str(uuid4())
    w.conn.execute(t.study_sessions.insert().values(id=sid,user_id=w.user,task_id=task,status='ended',
        started_at=started,ended_at=started+timedelta(seconds=seconds),measured_seconds=seconds,
        study_date=day,created_at=started))
    record=w.conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id==task))
    w.conn.execute(update(t.study_records).where(t.study_records.c.id==record).values(session_id=sid))
    if not snapshot:
        return task,sid,record
    at=started-timedelta(hours=1)
    repo=ProposalRepository(w.conn)
    row=repo.create(w.user,kind='initial_plan',target={'scope':'independent-pace'},normalized_input={},
        base_date=day,dependency_versions={},changes={'schemaVersion':'plan-placements-v1','placements':[
            {'id':task,'parts':[{'unitId':unit['id'],'materialId':unit['material_id'],'from':1,'to':10,
                'baseline':[5,2],'multiplier':list(multiplier)}]}]},preview_response={},now=at,
        proposal_type='initial',description='Independent historical placement',base_plan_id=w.plan['id'],base_plan_revision=1)
    repo.mark_applied(w.user,row['proposal_id'],expected_revision=0,response={'approved':True},now=at)
    return task,sid,record


@pytest.fixture
def case(progress_world,end_focus):
    w=progress_world;w.conn.commit();end_focus(w.user)
    unit=w.unit(1,10,difficulty='low')
    rows=[evidence(w,unit,w.day-timedelta(days=offset)) for offset in range(3)]
    future=w.task((unit,1,10),day=w.day+timedelta(days=1))
    return w,unit,rows,future


def home(client,w):
    w.conn.commit()
    result=client.get('/api/today',headers={'X-Anonymous-Id':w.user})
    assert result.status_code==200,result.text
    return result.json()['data']


def apply(client,w,calibration,accepted=True):
    w.conn.commit()
    return client.post('/api/plan/pace',headers={'X-Anonymous-Id':w.user},json={
        'subjectId':w.subject['id'],'calibrationId':calibration['calibrationId'],'apply':accepted})


@pytest.mark.parametrize('seconds,minutes',[ (600,10),(6000,100) ])
def test_untimed_future_outside_placement_duration_requires_recovery(api_client,case,seconds,minutes):
    w,unit,rows,future=case
    w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id.in_([row[1] for row in rows])).values(measured_seconds=seconds))
    value=home(api_client,w)['paceAlert']
    assert next(row for row in value['rows'] if row['taskId']==future)['after']==minutes
    response=apply(api_client,w,value)
    assert response.status_code==200,response.text
    assert response.json()['data']['replan'] is True
    assert response.json()['data']['overDays']==[(w.day+timedelta(days=1)).isoformat()]
    pending=w.conn.execute(select(t.plan_change_requests).where(t.plan_change_requests.c.user_id==w.user,
        t.plan_change_requests.c.kind=='recovery',t.plan_change_requests.c.status=='ready')).mappings().one()
    assert pending['recovery_context']['calibrationId']==value['calibrationId']


@pytest.mark.parametrize('state',['running','paused'])
def test_real_focus_change_after_preview_rejects_approval_and_preserves_facts(api_client,case,state):
    w,unit,rows,future=case
    today=w.task((unit,1,10))
    value=home(api_client,w)['paceAlert']
    response=api_client.post('/api/study/sessions',headers={'X-Anonymous-Id':w.user},json={'taskId':today})
    assert response.status_code==200,response.text
    sid=response.json()['data']['id']
    if state=='paused':
        response=api_client.post('/api/study/sessions/'+sid+'/pause',headers={'X-Anonymous-Id':w.user})
        assert response.status_code==200,response.text
    before=w.conn.execute(select(t.study_tasks)).all()
    result=apply(api_client,w,value)
    assert result.status_code==409,result.text
    assert result.json()['error']['code']=='PREVIEW_STALE'
    assert w.conn.execute(select(t.study_tasks)).all()==before
    assert w.conn.scalar(select(t.subjects.c.minutes_per_page).where(t.subjects.c.id==w.subject['id']))==2.5
    assert w.conn.scalar(select(t.study_sessions.c.status).where(t.study_sessions.c.id==sid))==state


def test_later_p2_request_prevents_old_pace_approval_without_consuming_another_offer(api_client,case):
    w,unit,rows,future=case
    value=home(api_client,w)['paceAlert']
    rid=str(uuid4())
    w.conn.execute(t.plan_change_requests.insert().values(id=rid,user_id=w.user,base_plan_id=w.plan['id'],
        base_plan_revision=1,kind='recovery',status='ready',input_payload={},
        recovery_context={'subjects':[],'tasks':[]},created_at=w.now,updated_at=w.now))
    before=w.conn.execute(select(t.study_tasks)).all()
    response=apply(api_client,w,value)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert home(api_client,w)['paceAlert'] is None
    assert w.conn.execute(select(t.study_tasks)).all()==before
    assert list(w.conn.scalars(select(t.speed_calibrations.c.id).where(t.speed_calibrations.c.subject_id==w.subject['id'])))==[value['calibrationId']]


def test_applied_reply_survives_date_plan_and_live_difficulty_changes(api_client,case):
    w,unit,rows,future=case
    value=home(api_client,w)['paceAlert'];reply=apply(api_client,w,value)
    assert reply.status_code==200,reply.text
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day+timedelta(days=1)))
    w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(difficulty='high'))
    w.conn.execute(update(t.plans).where(t.plans.c.id==w.plan['id']).values(revision=100))
    before=w.conn.execute(select(t.study_tasks)).all()
    assert apply(api_client,w,value).json()==reply.json()
    assert w.conn.execute(select(t.study_tasks)).all()==before
    applied=w.conn.execute(select(t.plan_proposals.c.changes).join(t.plan_proposal_states,
        t.plan_proposal_states.c.proposal_id==t.plan_proposals.c.id).where(
        t.plan_proposal_states.c.kind=='speed_adjust',t.plan_proposal_states.c.user_id==w.user)).scalar_one()
    part=applied['placements'][0]['parts'][0]
    assert part['baseline']==[3,1] and part['multiplier']==[1,1]
    assert w.conn.scalar(select(t.study_tasks.c.estimated_minutes).where(t.study_tasks.c.origin_task_id==future))==30


@pytest.mark.parametrize('invalid',['auto_stopped','calibration_excluded','outside_window','zero_completion','late_snapshot'])
def test_one_invalid_day_cannot_be_replaced_by_extra_sessions_today(api_client,case,invalid):
    w,unit,rows,future=case
    evidence(w,unit,w.day,kind='remedial')
    task,sid,record=rows[2]
    if invalid=='auto_stopped':w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==sid).values(status='auto_stopped'))
    if invalid=='calibration_excluded':w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==sid).values(excluded_from_calibration=True))
    if invalid=='outside_window':w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==sid).values(study_date=w.day-timedelta(days=3)))
    if invalid=='zero_completion':w.conn.execute(t.study_record_ranges.delete().where(t.study_record_ranges.c.record_id==record))
    if invalid=='late_snapshot':
        start=w.conn.scalar(select(t.study_sessions.c.started_at).where(t.study_sessions.c.id==sid))
        w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==sid).values(started_at=start-timedelta(hours=2)))
    assert home(api_client,w)['paceAlert'] is None
    assert w.conn.scalar(select(t.speed_calibrations.c.id).where(t.speed_calibrations.c.subject_id==w.subject['id'])) is None


def test_historical_high_multiplier_and_partial_completion_use_exact_boundary(api_client,progress_world,end_focus):
    w=progress_world;w.conn.commit();end_focus(w.user)
    unit=w.unit(1,10,difficulty='high')
    rows=[evidence(w,unit,w.day-timedelta(days=i),seconds=1350,multiplier=(3,2)) for i in range(3)]
    for task,sid,record in rows:
        w.conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==record).values(end_page=5))
    w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(difficulty='low'))
    value=home(api_client,w)['paceAlert']
    assert (value['perPage'],value['expectedPerPage'],value['ratio'])==(4.5,3.75,1.2)
    assert apply(api_client,w,value,False).status_code==200
    assert home(api_client,w)['paceAlert'] is None
    assert w.conn.scalar(select(t.subjects.c.minutes_per_page).where(t.subjects.c.id==w.subject['id']))==2.5


@pytest.mark.parametrize('break_lineage',[False,True])
def test_three_day_carried_lineage_uses_original_immutable_factors(api_client,progress_world,end_focus,break_lineage):
    w=progress_world;w.conn.commit();end_focus(w.user)
    unit=w.unit(1,20,difficulty='low')
    parent,_,_=evidence(w,unit,w.day-timedelta(days=3),seconds=2700,multiplier=(3,2))
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==parent).values(status='carried_over'))
    block=w.conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id==parent))
    for offset in range(3):
        task,_,_=evidence(w,unit,w.day-timedelta(days=offset),seconds=2700,snapshot=False)
        fields={'origin_task_id':parent}
        if not break_lineage or offset!=2:fields['block_id']=block
        w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(**fields))
    value=home(api_client,w)['paceAlert']
    if break_lineage:
        assert value is None
    else:
        assert value['perPage']==4.5 and value['expectedPerPage']==3.75 and value['ratio']==1.2
        assert apply(api_client,w,value).status_code==200
        assert w.conn.scalar(select(t.subjects.c.minutes_per_page).where(t.subjects.c.id==w.subject['id']))==3


def test_concurrent_approval_cannot_duplicate_task_replacement(api_client,case):
    from concurrent.futures import ThreadPoolExecutor
    w,unit,rows,future=case
    value=home(api_client,w)['paceAlert']
    before=w.conn.scalar(select(t.plans.c.revision).where(t.plans.c.id==w.plan['id']))
    w.conn.commit()
    def request():
        return api_client.post('/api/plan/pace',headers={'X-Anonymous-Id':w.user},json={
            'subjectId':w.subject['id'],'calibrationId':value['calibrationId'],'apply':True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:request(),range(2)))
    assert any(response.status_code==200 for response in responses)
    assert all(response.status_code in (200,409) for response in responses),[response.text for response in responses]
    assert request().status_code==200
    assert w.conn.scalar(select(t.plans.c.revision).where(t.plans.c.id==w.plan['id']))==before+1
    assert len(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==future)).all())==1
