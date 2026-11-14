from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.e2e.test_schedule_delete import user
from tests.test_progress_contract import progress_world
from tests.test_onboarding_events import add_event,add_exception,at
from tests.test_plan_draft import TODAY,NOW,plan_case
from tests.test_plan_addition_e2e import addition_case


@pytest.fixture
def calendar_world(progress_world):
    w=progress_world
    w.conn.execute(update(t.calendar_events).where(t.calendar_events.c.user_id==w.user).values(deleted_at=w.now))
    w.conn.commit()
    return w


def preview(client,owner,key,event,*,scope='once',revision=1):
    return client.patch(f'/api/schedules/{key}/preview',headers={'X-Anonymous-Id':owner},
        json={'scope':scope,'expectedRevision':revision,'event':event})


def apply(client,owner,key,identifier):
    return client.patch(f'/api/schedules/{key}',headers={'X-Anonymous-Id':owner},json={'previewId':identifier})


def test_once_move_keeps_original_occurrence_identity_and_replays(api_client,calendar_world):
    w=calendar_world;event=add_event(w.conn,user(w),day=w.day,start='12:00',end='13:00');w.conn.commit()
    key=f"{event['id']}@{w.day}"
    candidate=preview(api_client,w.user,key,{'date':(w.day+timedelta(days=1)).isoformat(),'start':'14:00','end':'15:00'})
    assert candidate.status_code==200,candidate.text
    assert candidate.json()['data']['moves']==[]
    assert w.conn.execute(select(t.calendar_event_exceptions)).all()==[];w.conn.commit()
    response=apply(api_client,w.user,key,candidate.json()['data']['previewId']);assert response.status_code==200,response.text
    assert response.json()['data']=={'applied':True}
    calendar=api_client.get('/api/calendar',headers={'X-Anonymous-Id':w.user},params={'from':w.day.isoformat(),'to':(w.day+timedelta(days=1)).isoformat()}).json()['data']
    row=next(x for x in calendar['fixed'] if x['scheduleId']==event['id'])
    assert (row['id'],row['date'],row['start'],row['revision'])==(key,(w.day+timedelta(days=1)).isoformat(),'14:00',2)
    assert apply(api_client,w.user,key,candidate.json()['data']['previewId']).json()==response.json()


def test_future_changes_transfer_compatible_exceptions_and_keep_discard_history(api_client,calendar_world):
    w=calendar_world;event=add_event(w.conn,user(w),day=w.day-timedelta(days=7),start='12:00',end='13:00',recurrence_rule='FREQ=WEEKLY;BYDAY=MO,WE')
    old=add_exception(w.conn,event,w.day-timedelta(days=7),kind='cancelled')
    discard=add_exception(w.conn,event,w.day+timedelta(days=2),kind='cancelled')
    transfer=add_exception(w.conn,event,w.day+timedelta(days=7),kind='moved',moved_day=w.day+timedelta(days=8),start='14:00',end='15:00')
    w.conn.commit();key=f"{event['id']}@{w.day}"
    candidate=preview(api_client,w.user,key,{'kind':'job','title':'새 반복','days':[1],'startDate':w.day.isoformat(),
        'start':'16:00','end':'17:00','until':None},scope='future')
    assert candidate.status_code==200,candidate.text
    actions={x['id']:x['action'] for x in candidate.json()['data']['exceptionChanges']}
    assert actions=={old['id']:'keep',discard['id']:'discard',transfer['id']:'transfer'}
    assert apply(api_client,w.user,key,candidate.json()['data']['previewId']).status_code==200
    changed=w.conn.execute(select(t.calendar_event_exceptions).where(t.calendar_event_exceptions.c.id==transfer['id'])).mappings().one()
    assert changed['event_id']!=event['id'] and changed['occurrence_date']==transfer['occurrence_date']
    assert w.conn.scalar(select(t.calendar_event_exceptions.c.event_id).where(t.calendar_event_exceptions.c.id==discard['id']))==event['id']
    assert w.conn.scalar(select(t.calendar_events.c.recurrence_until).where(t.calendar_events.c.id==event['id']))==w.day-timedelta(days=1)


def test_overlap_and_stale_revision_never_mutate_calendar(api_client,calendar_world):
    w=calendar_world;event=add_event(w.conn,user(w),day=w.day,start='12:00',end='13:00')
    add_event(w.conn,user(w),day=w.day,start='14:00',end='15:00');before=w.conn.execute(select(t.calendar_events)).all();w.conn.commit()
    key=f"{event['id']}@{w.day}"
    response=preview(api_client,w.user,key,{'date':w.day.isoformat(),'start':'14:30','end':'15:30'})
    assert response.status_code==409 and response.json()['error']['code']=='SCHEDULE_CONFLICT'
    assert preview(api_client,w.user,key,{'date':w.day.isoformat(),'start':'16:00','end':'17:00'},revision=2).status_code==409
    assert w.conn.execute(select(t.calendar_events)).all()==before


def test_partial_record_is_preserved_and_only_remaining_pages_move(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    task=case['old_task']
    owner=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(scheduled_date=TODAY,
        starts_at=at(TODAY,'09:00'),ends_at=at(TODAY,'09:50'),status='partial'))
    record=str(uuid4())
    conn.execute(t.study_records.insert().values(id=record,task_id=task['id'],result='partial',study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=5))
    event=add_event(conn,owner,day=TODAY,start='15:00',end='16:00')
    facts=(conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all());conn.commit()
    key=f"{event['id']}@{TODAY}"
    candidate=preview(api_client,case['user'],key,{'date':TODAY.isoformat(),'start':'09:00','end':'10:00'})
    assert candidate.status_code==200,candidate.text
    moved=next(x for x in candidate.json()['data']['moves'] if x['taskId']==task['id'])
    assert sorted(page for piece in moved['pieces'] for page in range(piece['from'],piece['to']+1))==list(range(6,21))
    assert apply(api_client,case['user'],key,candidate.json()['data']['previewId']).status_code==200
    assert (conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all())==facts
    rows=conn.execute(select(t.study_task_ranges).join(t.study_tasks).where(t.study_tasks.c.status=='planned')).mappings().all()
    assert sorted(page for row in rows for page in range(row['start_page'],row['end_page']+1))==list(range(6,21))


@pytest.mark.parametrize('bad_event', [
    {'date':'2026-11-09','start':'15:00','end':'14:00'},
    {'date':'2026-11-09','start':'23:30','end':'00:30'},
    {'date':'2026-02-30','start':'14:00','end':'15:00'},
])
def test_invalid_time_shape_has_no_candidate(api_client,calendar_world,bad_event):
    w=calendar_world;event=add_event(w.conn,user(w),day=w.day);w.conn.commit()
    response=preview(api_client,w.user,f"{event['id']}@{w.day}",bad_event)
    assert response.status_code==422


def test_apply_failure_rolls_back_event_and_keeps_preview_retryable(api_client,calendar_world,monkeypatch):
    from app.services import schedule_changes
    w=calendar_world;event=add_event(w.conn,user(w),day=w.day,start='12:00',end='13:00');w.conn.commit()
    key=f"{event['id']}@{w.day}"
    candidate=preview(api_client,w.user,key,{'date':w.day.isoformat(),'start':'14:00','end':'15:00'}).json()['data']['previewId']
    original=schedule_changes.EDIT_DOMAIN
    from dataclasses import replace
    def fail(*_): raise RuntimeError('injected after-apply failure')
    monkeypatch.setattr(schedule_changes,'EDIT_DOMAIN',replace(original,after_apply=fail))
    assert apply(api_client,w.user,key,candidate).status_code==500
    assert w.conn.scalar(select(t.calendar_events.c.revision).where(t.calendar_events.c.id==event['id']))==1
    assert w.conn.execute(select(t.calendar_event_exceptions)).all()==[];w.conn.commit()
    monkeypatch.setattr(schedule_changes,'EDIT_DOMAIN',original)
    assert apply(api_client,w.user,key,candidate).status_code==200
