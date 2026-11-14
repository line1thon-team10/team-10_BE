"""Independent HTTP checks for authenticated, transactional date settlement."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.user import User
from app.repositories.users import UserRepository
from app.services.planner import Planner
from tests.test_progress_contract import progress_world


@pytest.fixture
def clock(monkeypatch):
    instant=[datetime(2026,11,10,16,tzinfo=timezone.utc)]  # November 11 in Seoul.
    monkeypatch.setattr('app.services.day_rollover.now_utc',lambda:instant[0])
    monkeypatch.setattr('app.services.focus.now',lambda:instant[0])
    return instant


def ordinary(w, *, last=date(2026,11,8), zone='Asia/Seoul'):
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(demo_date=None,last_assessed_date=last,timezone=zone))
    w.conn.commit()


def get(client,user,path='/api/books'):
    return client.get(path,headers={'X-Anonymous-Id':user})


def state(conn):
    return {table.name:conn.execute(select(table)).all() for table in (t.users,t.subjects,t.plans,
        t.study_tasks,t.study_task_ranges,t.study_records,t.study_record_ranges,t.study_sessions,
        t.study_session_segments,t.daily_plan_totals,t.user_notices,t.plan_change_requests,t.quiz_sessions)}


@pytest.fixture
def ordinary_case(progress_world,end_focus,clock):
    w=progress_world;unit=w.unit(1,100)
    task=w.task((unit,1,10),done=[(unit,1,9)],status='partial')
    w.task((unit,11,20),day=w.day+timedelta(days=1))
    w.conn.commit();end_focus(w.user);ordinary(w)
    return w,unit,task


def test_endpoint_write_failure_does_not_undo_committed_date_settlement(api_client,ordinary_case,monkeypatch):
    from app.services.subjects import SubjectService
    w,unit,task=ordinary_case
    original=w.conn.scalar(select(t.subjects.c.name).where(t.subjects.c.id==w.subject['id']))
    w.conn.commit()
    def fail(self,name):
        self.conn.execute(update(t.subjects).where(t.subjects.c.id==w.subject['id']).values(name='must rollback'))
        raise RuntimeError('endpoint failure after a write')
    monkeypatch.setattr(SubjectService,'create',fail)
    response=api_client.post('/api/subjects',headers={'X-Anonymous-Id':w.user},json={'name':'failure'})
    assert response.status_code==500,response.text
    assert w.conn.scalar(select(t.subjects.c.name).where(t.subjects.c.id==w.subject['id']))==original
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==date(2026,11,10)
    dates=set(w.conn.scalars(select(t.daily_plan_totals.c.study_date).where(t.daily_plan_totals.c.user_id==w.user,
        t.daily_plan_totals.c.finalized_at.is_not(None))))
    assert {date(2026,11,9),date(2026,11,10)}<=dates
    after=state(w.conn);w.conn.commit()
    assert get(api_client,w.user).status_code==200
    assert state(w.conn)==after


def test_second_date_failure_rolls_back_timeout_and_all_prior_closed_dates(api_client,progress_world,clock,monkeypatch):
    w=progress_world;unit=w.unit(1,100);task=w.task((unit,1,10))
    sid=w.conn.scalar(select(t.study_sessions.c.id).where(t.study_sessions.c.user_id==w.user,t.study_sessions.c.status=='running'))
    start=clock[0]-timedelta(minutes=90)
    w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==sid).values(task_id=task,started_at=start,
        expected_seconds=600,last_confirmed_elapsed_seconds=0,check_prompted_at=None,study_date=w.day))
    w.conn.execute(update(t.study_session_segments).where(t.study_session_segments.c.session_id==sid).values(started_at=start,ended_at=None))
    ordinary(w)
    before=state(w.conn);w.conn.commit()
    original=Planner.close_day
    def fail(self,day,**kwargs):
        result=original(self,day,**kwargs)
        if day==date(2026,11,10):raise RuntimeError('mid catch-up failure')
        return result
    monkeypatch.setattr(Planner,'close_day',fail)
    assert get(api_client,w.user).status_code==500
    assert state(w.conn)==before
    w.conn.commit();monkeypatch.setattr(Planner,'close_day',original)
    assert get(api_client,w.user).status_code==200
    saved=w.conn.execute(select(t.study_sessions).where(t.study_sessions.c.id==sid)).mappings().one()
    assert saved['status']=='auto_stopped' and saved['measured_seconds']==1200 and saved['excluded_from_calibration']


def test_invalid_credentials_do_not_settle_anyone_and_owner_settlement_is_isolated(api_client,ordinary_case,clock):
    w,unit,task=ordinary_case
    other=User.anonymous(now=datetime(2026,11,1,tzinfo=timezone.utc))
    UserRepository(w.conn).add(other)
    w.conn.commit();before=state(w.conn);w.conn.commit()
    for headers in ({},{'X-Anonymous-Id':'not-a-uuid'},{'X-Anonymous-Id':str(uuid4())},
        [('X-Anonymous-Id',w.user),('X-Anonymous-Id',other.id)]):
        assert api_client.get('/api/books',headers=headers).status_code==401
    assert state(w.conn)==before
    w.conn.commit()
    assert get(api_client,other.id).status_code==200
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==other.id))==date(2026,11,10)
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==date(2026,11,8)
    assert w.conn.execute(select(t.study_tasks)).all()==before['study_tasks']


@pytest.mark.parametrize('zone,last,instant,expected',[
    ('America/New_York',date(2026,10,30),datetime(2026,11,2,5,tzinfo=timezone.utc),[date(2026,10,31),date(2026,11,1)]),
    ('America/New_York',date(2026,3,6),datetime(2026,3,9,4,tzinfo=timezone.utc),[date(2026,3,7),date(2026,3,8)]),
    ('Asia/Seoul',date(2026,12,30),datetime(2027,1,1,15,tzinfo=timezone.utc),[date(2026,12,31),date(2027,1,1)]),
])
def test_catchup_counts_local_dates_not_twenty_four_hour_intervals(api_client,progress_world,end_focus,clock,monkeypatch,zone,last,instant,expected):
    w=progress_world;w.conn.commit();end_focus(w.user);ordinary(w,last=last,zone=zone)
    clock[0]=instant
    seen=[];original=Planner.close_day
    def traced(self,day,**kwargs):
        seen.append(day);return original(self,day,**kwargs)
    monkeypatch.setattr(Planner,'close_day',traced)
    response=get(api_client,w.user)
    assert response.status_code==200,response.text
    assert seen==expected
    before=state(w.conn);w.conn.commit()
    clock[0]-=timedelta(days=5)
    assert get(api_client,w.user).status_code==200
    assert state(w.conn)==before and seen==expected


def test_exam_next_day_archives_without_rewriting_remedial_originals(api_client,progress_world,end_focus,clock):
    w=progress_world;unit=w.unit(1,100)
    exam=date(2026,11,10)
    study=w.task((unit,1,10),day=exam)
    supplement=w.task((unit,11,20),kind='remedial',day=exam+timedelta(days=1))
    record=w.task((unit,21,30),done=[(unit,21,30)],status='completed')
    w.conn.execute(update(t.subjects).where(t.subjects.c.id==w.subject['id']).values(exam_date=exam))
    supplement_before=w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==supplement)).one()
    records_before=w.conn.execute(select(t.study_records)).all()
    ranges_before=w.conn.execute(select(t.study_record_ranges)).all()
    w.conn.commit();end_focus(w.user);ordinary(w)
    clock[0]=datetime(2026,11,9,15,tzinfo=timezone.utc)
    assert get(api_client,w.user).status_code==200
    assert w.conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==w.subject['id']))=='active'
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==study))=='excluded'
    w.conn.commit();clock[0]+=timedelta(days=1)
    assert get(api_client,w.user).status_code==200
    assert w.conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==w.subject['id']))=='archived'
    assert w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==supplement)).one()==supplement_before
    assert w.conn.execute(select(t.study_records)).all()==records_before
    assert w.conn.execute(select(t.study_record_ranges)).all()==ranges_before
    w.conn.commit();clock[0]+=timedelta(days=2)
    assert get(api_client,w.user).status_code==200
    assert w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==supplement)).one()==supplement_before


def test_demo_key_replays_despite_real_clock_jump_without_automatic_catchup(api_client,progress_world,end_focus,clock):
    w=progress_world;w.conn.commit();end_focus(w.user)
    key=str(uuid4());headers={'X-Anonymous-Id':w.user,'Idempotency-Key':key}
    first=api_client.post('/api/demo/next-day',headers=headers)
    assert first.status_code==200,first.text
    before=state(w.conn);w.conn.commit();clock[0]=datetime(2035,1,1,tzinfo=timezone.utc)
    assert get(api_client,w.user).status_code==200
    assert api_client.post('/api/demo/next-day',headers=headers).json()==first.json()
    assert state(w.conn)==before


def test_overlapping_arrivals_do_not_close_any_date_twice(api_client,ordinary_case,clock,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    w,unit,task=ordinary_case
    entered,release=Event(),Event();seen=[];original=Planner.close_day
    def wait(self,day,**kwargs):
        seen.append(day)
        if len(seen)==1:
            entered.set();assert release.wait(10)
        return original(self,day,**kwargs)
    monkeypatch.setattr(Planner,'close_day',wait)
    if w.conn.dialect.name=='sqlite':
        from app.db import session
        with session.engine.connect() as conn:conn.exec_driver_sql('PRAGMA busy_timeout=20')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(get,api_client,w.user)
        assert entered.wait(10)
        try:
            second=get(api_client,w.user)
            assert second.status_code==409,second.text
        finally:release.set()
        assert first.result(timeout=15).status_code==200
    assert get(api_client,w.user).status_code==200
    assert seen==[date(2026,11,9),date(2026,11,10)]


@pytest.mark.parametrize('paused',[False,True])
def test_focus_started_through_http_keeps_identity_across_ordinary_midnight(api_client,progress_world,end_focus,clock,paused):
    w=progress_world;unit=w.unit(1,100);task=w.task((unit,1,10))
    w.conn.commit();end_focus(w.user);ordinary(w,zone='UTC')
    clock[0]=datetime(2026,11,9,23,55,tzinfo=timezone.utc)
    headers={'X-Anonymous-Id':w.user}
    response=api_client.post('/api/study/sessions',headers=headers,json={'taskId':task})
    assert response.status_code==200,response.text
    sid=response.json()['data']['id']
    if paused:
        clock[0]+=timedelta(minutes=3)
        response=api_client.post('/api/study/sessions/'+sid+'/pause',headers=headers)
        assert response.status_code==200,response.text
    clock[0]=datetime(2026,11,10,0,10,tzinfo=timezone.utc)
    response=api_client.get('/api/study/sessions/active',headers=headers)
    assert response.status_code==200,response.text
    view=response.json()['data']
    assert (view['id'],view['taskId'])==(sid,task)
    assert (view['minutes'],view['todayMinutes'])==((3,0) if paused else (15,10))
    assert view['status']==('paused' if paused else 'active')
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==task))=='planned'
    assert not w.conn.execute(select(t.study_tasks.c.id).where(t.study_tasks.c.origin_task_id==task)).first()
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==w.day
