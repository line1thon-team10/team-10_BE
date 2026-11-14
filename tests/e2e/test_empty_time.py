from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import func, select, update
from app.db import schema as t
from app.services.empty_time import EmptyTimeService
from tests.test_progress_contract import progress_world


def offer(world,*,slots=None):
    user=dict(world.conn.execute(select(t.users).where(t.users.c.id==world.user)).mappings().one())
    result=EmptyTimeService(world.conn,user).offer(slots or [{'date':world.day.isoformat(),'start':'09:00','end':'10:00','label':'removed'}])
    world.conn.commit()
    return result


def apply(client,world,candidate,option):
    return client.post('/api/plan/empty-time',headers={'X-Anonymous-Id':world.user},json={'previewId':candidate['previewId'],'option':option})


def test_rest_replays_once_and_conflicting_choice_is_rejected(api_client,progress_world):
    w=progress_world
    before=w.conn.execute(select(t.study_tasks)).all()
    candidate=offer(w)
    assert {x['key'] for x in candidate['options']}=={'rest','pull','advance'}
    assert not next(x for x in candidate['options'] if x['key']=='pull')['enabled']
    assert apply(api_client,w,candidate,'pull').status_code==409
    first=apply(api_client,w,candidate,'rest')
    assert first.status_code==200 and first.json()['data']=={'applied':True}
    assert apply(api_client,w,candidate,'rest').json()==first.json()
    assert apply(api_client,w,candidate,'advance').status_code==409
    assert w.conn.execute(select(t.study_tasks)).all()==before


def test_pull_preserves_durable_block_ranges_and_all_historical_records(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10)
    identifier=w.task((unit,1,10),day=w.day+timedelta(days=1))
    source=dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==identifier)).mappings().one())
    records=w.conn.execute(select(t.study_records)).all()
    ranges=w.conn.execute(select(t.study_record_ranges)).all()
    candidate=offer(w)
    assert next(x for x in candidate['options'] if x['key']=='pull')['enabled']
    result=apply(api_client,w,candidate,'pull');assert result.status_code==200,result.text
    moved=w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==identifier)).mappings().one()
    assert moved['block_id']==source['block_id'] and moved['scheduled_date']==w.day
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==identifier))=='carried_over'
    actual=w.conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==moved['id'])).mappings().one()
    assert (actual['unit_id'],actual['start_page'],actual['end_page'])==(unit['id'],1,10)
    assert w.conn.execute(select(t.study_records)).all()==records
    assert w.conn.execute(select(t.study_record_ranges)).all()==ranges
    w.conn.commit()
    assert apply(api_client,w,candidate,'pull').json()==result.json()
    assert w.conn.scalar(select(func.count()).select_from(t.study_tasks).where(t.study_tasks.c.origin_task_id==identifier))==1


@pytest.mark.parametrize('blocked',['record','short_gap','exam'])
def test_pull_disabled_for_recorded_task_small_gap_or_exam(api_client,progress_world,blocked):
    w=progress_world;unit=w.unit(1,10)
    w.task((unit,1,10),day=w.day+timedelta(days=1),done=[] if blocked=='record' else None)
    if blocked=='exam':
        w.conn.execute(update(t.subjects).where(t.subjects.c.id==w.subject['id']).values(exam_date=w.day))
    slots=[{'date':w.day.isoformat(),'start':'09:00','end':'09:14' if blocked=='short_gap' else '10:00'}]
    candidate=offer(w,slots=slots)
    assert not next(x for x in candidate['options'] if x['key']=='pull')['enabled']
    response=apply(api_client,w,candidate,'pull')
    assert response.status_code==409 and response.json()['error']['code']=='OPTION_DISABLED'


def test_advance_uses_only_unallocated_current_scope_and_preview_is_read_only(api_client,progress_world):
    w=progress_world;unit=w.unit(1,30,difficulty='low')
    w.task((unit,1,10),done=[(unit,1,5)],status='partial')
    w.task((unit,11,20),day=w.day+timedelta(days=1))
    before=w.conn.scalar(select(func.count()).select_from(t.study_tasks))
    candidate=offer(w,slots=[{'date':w.day.isoformat(),'start':'09:00','end':'10:00'}])
    assert w.conn.scalar(select(func.count()).select_from(t.study_tasks))==before
    w.conn.commit()
    response=apply(api_client,w,candidate,'advance');assert response.status_code==200,response.text
    rows=w.conn.execute(select(t.study_task_ranges).join(t.study_tasks).where(t.study_tasks.c.placement_reason=='schedule-change')).mappings().all()
    assert [(r['start_page'],r['end_page']) for r in rows]==[(21,30)]


def test_stale_owner_and_empty_slots(api_client,progress_world):
    w=progress_world;candidate=offer(w)
    other=api_client.post('/api/session').json()['data']['anonymousId']
    response=api_client.post('/api/plan/empty-time',headers={'X-Anonymous-Id':other},json={'previewId':candidate['previewId'],'option':'rest'})
    assert response.status_code==404
    w.conn.execute(update(t.plans).where(t.plans.c.id==w.plan['id']).values(revision=t.plans.c.revision+1));w.conn.commit()
    assert apply(api_client,w,candidate,'rest').status_code==409
    empty=offer(w,slots=[{'date':(w.day-timedelta(days=1)).isoformat(),'start':'09:00','end':'10:00'}])
    assert empty=={'emptyTime':None,'slots':[],'options':[],'previewId':None}



def test_unplaceable_legacy_task_never_exposes_fake_free_time(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10)
    task=w.task((unit,1,10))
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(estimated_minutes=901))
    response=offer(w)
    assert response=={'emptyTime':None,'slots':[],'options':[],'previewId':None}



@pytest.mark.parametrize('clock,expected',[('08:00',('08:00','10:00')),('09:30',('09:30','10:00')),('23:00',None),('23:30',None)])
def test_real_today_free_time_starts_at_server_clock(progress_world,monkeypatch,clock,expected):
    from app.services import schedule_placements
    from tests.test_onboarding_events import at
    w=progress_world
    current=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    current['demo_date']=None
    w.conn.execute(update(t.calendar_events).where(t.calendar_events.c.user_id==w.user).values(deleted_at=w.now))
    monkeypatch.setattr(schedule_placements,'now_utc',lambda:at(w.day,clock))
    slots=schedule_placements.free_calendar_slots(w.conn,current,[{'date':w.day.isoformat(),'start':'08:00','end':'10:00'}])
    if expected is None: assert slots==()
    else: assert (schedule_placements.clock_text(slots[0].start),schedule_placements.clock_text(slots[0].end))==expected


def test_demo_date_has_no_invented_wall_clock_cutoff(progress_world,monkeypatch):
    from app.services import schedule_placements
    from tests.test_onboarding_events import at
    w=progress_world
    current=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    w.conn.execute(update(t.calendar_events).where(t.calendar_events.c.user_id==w.user).values(deleted_at=w.now))
    monkeypatch.setattr(schedule_placements,'now_utc',lambda:at(w.day,'23:30'))
    slots=schedule_placements.free_calendar_slots(w.conn,current,[{'date':w.day.isoformat(),'start':'08:00','end':'10:00'}])
    assert slots and schedule_placements.clock_text(slots[0].start)=='08:00'
