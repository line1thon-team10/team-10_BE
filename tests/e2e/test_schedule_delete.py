from datetime import timedelta
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_onboarding_events import add_event,add_exception


def user(w):
    return dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())


def preview(client,w,key,scope='once',revision=1):
    return client.delete(f'/api/schedules/{key}/preview',headers={'X-Anonymous-Id':w.user},params={'scope':scope,'expectedRevision':revision})


def apply(client,w,key,identifier):
    return client.delete(f'/api/schedules/{key}',headers={'X-Anonymous-Id':w.user},params={'previewId':identifier})


def test_delete_preview_only_then_parent_replay_and_fresh_empty_time(api_client,progress_world):
    w=progress_world;event=add_event(w.conn,user(w),day=w.day,start='12:00',end='13:00')
    key=f"{event['id']}@{w.day}"
    facts=(w.conn.execute(select(t.study_tasks)).all(),w.conn.execute(select(t.study_records)).all())
    w.conn.commit()
    candidate=preview(api_client,w,key);assert candidate.status_code==200,candidate.text
    assert w.conn.scalar(select(t.calendar_events.c.revision).where(t.calendar_events.c.id==event['id']))==1
    assert w.conn.execute(select(t.calendar_event_exceptions).where(t.calendar_event_exceptions.c.event_id==event['id'])).all()==[]
    w.conn.commit()
    response=apply(api_client,w,key,candidate.json()['data']['previewId']);assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['emptyTime']=={'date':w.day.isoformat(),'start':'12:00','end':'13:00','label':'비워진 시간'}
    assert data['previewId']!=candidate.json()['data']['previewId']
    assert apply(api_client,w,key,candidate.json()['data']['previewId']).json()==response.json()
    assert (w.conn.execute(select(t.study_tasks)).all(),w.conn.execute(select(t.study_records)).all())==facts
    w.conn.commit()
    rest=api_client.post('/api/plan/empty-time',headers={'X-Anonymous-Id':w.user},json={'previewId':data['previewId'],'option':'rest'})
    assert rest.status_code==200,rest.text


def test_future_delete_preserves_prior_occurrences_and_exception_rows(api_client,progress_world):
    w=progress_world;event=add_event(w.conn,user(w),day=w.day-timedelta(days=7),start='12:00',end='13:00',
        recurrence_rule='FREQ=WEEKLY;BYDAY=MO,WE')
    earlier=add_exception(w.conn,event,w.day-timedelta(days=7),kind='moved',moved_day=w.day-timedelta(days=6))
    later=add_exception(w.conn,event,w.day+timedelta(days=2),kind='moved',moved_day=w.day+timedelta(days=3))
    exceptions=w.conn.execute(select(t.calendar_event_exceptions).where(t.calendar_event_exceptions.c.event_id==event['id'])).all()
    w.conn.commit();key=f"{event['id']}@{w.day}"
    candidate=preview(api_client,w,key,'future');assert candidate.status_code==200,candidate.text
    response=apply(api_client,w,key,candidate.json()['data']['previewId']);assert response.status_code==200,response.text
    assert w.conn.scalar(select(t.calendar_events.c.recurrence_until).where(t.calendar_events.c.id==event['id']))==w.day-timedelta(days=1)
    assert w.conn.execute(select(t.calendar_event_exceptions).where(t.calendar_event_exceptions.c.event_id==event['id'])).all()==exceptions
    w.conn.commit()
    calendar=api_client.get('/api/calendar',headers={'X-Anonymous-Id':w.user},params={'from':(w.day-timedelta(days=7)).isoformat(),'to':(w.day+timedelta(days=7)).isoformat()})
    assert calendar.status_code==200,calendar.text
    fixed=[x for x in calendar.json()['data']['fixed'] if x['scheduleId']==event['id']]
    assert any(x['id'].endswith('@'+(w.day-timedelta(days=7)).isoformat()) for x in fixed)
    assert all(x['date']<w.day.isoformat() for x in fixed)


def test_revision_foreign_nonoccurrence_and_past_are_rejected(api_client,progress_world):
    w=progress_world;event=add_event(w.conn,user(w),day=w.day,start='12:00',end='13:00');w.conn.commit()
    key=f"{event['id']}@{w.day}"
    assert preview(api_client,w,key,revision=2).status_code==409
    assert preview(api_client,w,key,'future').status_code==422
    assert preview(api_client,w,f"{event['id']}@{w.day+timedelta(days=1)}").status_code==404
    other=api_client.post('/api/session').json()['data']['anonymousId']
    response=api_client.delete(f'/api/schedules/{key}/preview',headers={'X-Anonymous-Id':other},params={'scope':'once','expectedRevision':1})
    assert response.status_code==404
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=w.day+timedelta(days=1)));w.conn.commit()
    assert preview(api_client,w,key).status_code==422


def test_second_preview_is_stale_after_first_approval(api_client,progress_world):
    w=progress_world;event=add_event(w.conn,user(w),day=w.day,start='12:00',end='13:00');w.conn.commit()
    key=f"{event['id']}@{w.day}"
    a=preview(api_client,w,key).json()['data']['previewId']
    b=preview(api_client,w,key).json()['data']['previewId']
    assert apply(api_client,w,key,a).status_code==200
    assert apply(api_client,w,key,b).status_code==409
