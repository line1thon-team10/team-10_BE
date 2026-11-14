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


def test_unpaged_review_moves_as_original_time_and_block_without_fake_pages(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    task,block=str(uuid4()),str(uuid4())
    conn.execute(t.study_blocks.insert().values(id=block,subject_id=case['subject'],unit_id=None,task_type='review',created_at=NOW))
    conn.execute(t.study_tasks.insert().values(id=task,block_id=block,subject_id=case['subject'],plan_id=case['old_task']['plan_id'],
        task_type='review',title='페이지 없는 총복습',scheduled_date=TODAY,starts_at=at(TODAY,'10:00'),ends_at=at(TODAY,'10:30'),
        estimated_minutes=30,status='planned',created_at=NOW))
    owner=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    event=add_event(conn,owner,day=TODAY,start='15:00',end='16:00');conn.commit()
    key=f"{event['id']}@{TODAY}"
    response=preview(api_client,case['user'],key,{'date':TODAY.isoformat(),'start':'10:00','end':'11:00'})
    assert response.status_code==200,response.text
    moved=next(x for x in response.json()['data']['moves'] if x['taskId']==task)
    assert len(moved['pieces'])==1 and moved['pieces'][0]['minutes']==30
    assert moved['pieces'][0]['pages']==0 and moved['pieces'][0]['from'] is None
    applied=apply(api_client,case['user'],key,response.json()['data']['previewId'])
    assert applied.status_code==200,applied.text
    row=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==task)).mappings().one()
    assert row['block_id']==block and row['estimated_minutes']==30
    assert conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==row['id'])).all()==[]


def test_calendar_reallocation_links_pending_obligation_without_replacing_original(api_client,domain_connection,addition_case):
    from tests.e2e.test_pending_obligation_allocation import pending
    conn,case=domain_connection,addition_case
    conn.execute(update(t.materials).where(t.materials.c.id==case['material']).values(page_count=60))
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(end_page=60))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['old_task']['id']).values(
        scheduled_date=TODAY,starts_at=at(TODAY,'09:00'),ends_at=at(TODAY,'09:50')))
    obligation=pending(conn,case,60)
    owner=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    event=add_event(conn,owner,day=TODAY,start='15:00',end='16:00');conn.commit()
    key=f"{event['id']}@{TODAY}"
    candidate=preview(api_client,case['user'],key,{'date':TODAY.isoformat(),'start':'09:00','end':'10:00'})
    assert candidate.status_code==200,candidate.text
    moved=next(row for row in candidate.json()['data']['moves'] if row['taskId']==case['old_task']['id'])
    assert sorted(page for piece in moved['pieces'] for page in range(piece['from'],piece['to']+1))==list(range(1,21))
    assert conn.execute(select(t.remedial_obligation_blocks)).all()==[];conn.commit()
    response=apply(api_client,case['user'],key,candidate.json()['data']['previewId'])
    assert response.status_code==200,response.text
    links=list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id).where(t.remedial_obligation_blocks.c.obligation_id==obligation)))
    assert len(links)>=3
    ranges=conn.execute(select(t.study_task_ranges).join(t.study_tasks,t.study_tasks.c.id==t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.block_id.in_(links))).mappings().all()
    assert sorted(page for row in ranges for page in range(row['start_page'],row['end_page']+1))==list(range(1,61))
    original=conn.execute(select(t.remedial_obligations).where(t.remedial_obligations.c.id==obligation)).mappings().one()
    assert original['required_ranges']==[[1,60]]
    before=conn.execute(select(t.remedial_obligation_blocks)).all();conn.commit()
    assert apply(api_client,case['user'],key,candidate.json()['data']['previewId']).json()==response.json()
    assert conn.execute(select(t.remedial_obligation_blocks)).all()==before


def test_edit_detects_overlap_with_fractional_minute_task_end(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    task=case['old_task']
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(scheduled_date=TODAY,
        starts_at=at(TODAY,'09:00'),ends_at=at(TODAY,'09:50')+timedelta(seconds=30)))
    owner=dict(conn.execute(select(t.users).where(t.users.c.id==case['user'])).mappings().one())
    event=add_event(conn,owner,day=TODAY,start='15:00',end='16:00');conn.commit()
    key=f"{event['id']}@{TODAY}"
    candidate=preview(api_client,case['user'],key,{'date':TODAY.isoformat(),'start':'09:50','end':'10:50'})
    assert candidate.status_code==200,candidate.text
    assert any(move['taskId']==task['id'] for move in candidate.json()['data']['moves'])
    response=apply(api_client,case['user'],key,candidate.json()['data']['previewId'])
    assert response.status_code==200,response.text
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==task['id']))=='carried_over'


def test_fractional_fixed_event_rejects_overlapping_schedule_edit(api_client,calendar_world):
    w=calendar_world
    event=add_event(w.conn,user(w),day=w.day,start='16:00',end='17:00')
    add_event(w.conn,user(w),day=w.day,start='12:00',end='13:00',ends_at=at(w.day,'13:00')+timedelta(seconds=30))
    w.conn.commit()
    response=preview(api_client,w.user,f"{event['id']}@{w.day}",{'date':w.day.isoformat(),'start':'13:00','end':'14:00'})
    assert response.status_code==409 and response.json()['error']['code']=='SCHEDULE_CONFLICT'
