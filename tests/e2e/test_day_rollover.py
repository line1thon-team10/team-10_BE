"""Normal-user HTTP reads settle local dates in a separate durable transaction."""
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.db import schema as t
from app.repositories.locking import lock_user
from app.services.planner import Planner
from tests.test_progress_contract import progress_world


@pytest.fixture
def real_clock(monkeypatch):
    from app.services import day_rollover, focus
    clock = [datetime(2026, 11, 10, 16, tzinfo=timezone.utc)]
    monkeypatch.setattr(day_rollover, 'now_utc', lambda: clock[0])
    monkeypatch.setattr(focus, 'now', lambda: clock[0])
    return clock


def ordinary(world, *, last=date(2026, 11, 8), zone='Asia/Seoul'):
    world.conn.execute(update(t.users).where(t.users.c.id == world.user).values(
        demo_date=None, last_assessed_date=last, timezone=zone))
    world.conn.commit()


def read(client, user, path='/api/books'):
    return client.get(path, headers={'X-Anonymous-Id': user})


def counts(conn):
    return {table.name: conn.scalar(select(func.count()).select_from(table)) for table in
            (t.study_tasks, t.daily_plan_totals, t.plan_change_requests, t.user_notices)}


def test_absent_days_close_in_order_and_repeated_reads_are_idempotent(api_client, progress_world, end_focus, real_clock, monkeypatch):
    w = progress_world
    unit = w.unit(1, 100)
    w.task((unit, 1, 10), done=[(unit, 1, 9)], status='partial')
    w.task((unit, 11, 20), day=w.day+timedelta(days=1))
    w.conn.commit(); end_focus(w.user)
    ordinary(w)
    seen = []
    close = Planner.close_day
    def traced(self, day, **kwargs):
        seen.append(day)
        return close(self, day, **kwargs)
    monkeypatch.setattr(Planner, 'close_day', traced)
    response = read(api_client, w.user)
    assert response.status_code == 200, response.text
    assert seen == [date(2026,11,9),date(2026,11,10)]
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user)) == date(2026,11,10)
    stored = counts(w.conn); w.conn.commit()
    assert read(api_client,w.user).status_code == 200
    assert seen == [date(2026,11,9),date(2026,11,10)] and counts(w.conn) == stored
    assert w.conn.scalar(select(t.users.c.demo_date).where(t.users.c.id==w.user)) is None


def test_failure_mid_catchup_rolls_back_every_closed_day(api_client, progress_world, end_focus, real_clock, monkeypatch):
    w = progress_world; unit=w.unit(1,100)
    w.task((unit,1,10)); w.conn.commit(); end_focus(w.user); ordinary(w)
    before=counts(w.conn); w.conn.commit()
    close=Planner.close_day
    def failure(self,day,**kwargs):
        result=close(self,day,**kwargs)
        if day==date(2026,11,10): raise RuntimeError('abort second day')
        return result
    monkeypatch.setattr(Planner,'close_day',failure)
    assert read(api_client,w.user).status_code==500
    assert counts(w.conn)==before
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==date(2026,11,8)
    w.conn.commit(); monkeypatch.setattr(Planner,'close_day',close)
    assert read(api_client,w.user).status_code==200


@pytest.mark.parametrize('zone,before,after', [
    ('Asia/Seoul',datetime(2026,11,9,14,59,tzinfo=timezone.utc),datetime(2026,11,9,15,tzinfo=timezone.utc)),
    ('America/New_York',datetime(2026,11,10,4,59,tzinfo=timezone.utc),datetime(2026,11,10,5,tzinfo=timezone.utc))])
def test_timezone_midnight_is_the_settlement_boundary(api_client,progress_world,end_focus,real_clock,zone,before,after):
    w=progress_world; w.conn.commit();end_focus(w.user);ordinary(w,zone=zone)
    real_clock[0]=before
    assert read(api_client,w.user).status_code==200
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==date(2026,11,8)
    w.conn.commit();real_clock[0]=after
    assert read(api_client,w.user).status_code==200
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==date(2026,11,9)


def test_demo_user_reads_never_advance_virtual_date(api_client,demo_user,domain_connection,real_clock):
    before=dict(domain_connection.execute(select(t.users).where(t.users.c.id==demo_user)).mappings().one())
    domain_connection.commit();real_clock[0]=datetime(2035,1,1,tzinfo=timezone.utc)
    assert read(api_client,demo_user).status_code==200
    assert dict(domain_connection.execute(select(t.users).where(t.users.c.id==demo_user)).mappings().one())==before


def test_new_unplanned_user_settlement_does_not_require_a_plan(api_client,domain_connection,real_clock,monkeypatch):
    from app.routers import session
    monkeypatch.setattr(session,'DEMO_MODE',False)
    user=api_client.post('/api/session').json()['data']['anonymousId']
    domain_connection.execute(update(t.users).where(t.users.c.id==user).values(created_at=datetime(2020,1,1,tzinfo=timezone.utc)))
    domain_connection.commit()
    assert read(api_client,user).status_code==200
    assert domain_connection.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==user))==date(2026,11,10)
    assert domain_connection.scalar(select(func.count()).select_from(t.plans).where(t.plans.c.user_id==user))==0


def test_owner_lock_blocks_overlap_without_advancing_date(api_client,progress_world,end_focus,real_clock):
    w=progress_world;w.conn.commit();end_focus(w.user);ordinary(w)
    if w.conn.dialect.name=='sqlite':
        from app.db import session
        with session.engine.connect() as conn: conn.exec_driver_sql('PRAGMA busy_timeout=20')
    lock_user(w.conn,UUID(w.user))
    try:
        response=read(api_client,w.user)
        assert response.status_code==409,response.text
        assert response.json()['error']['code']=='REQUEST_IN_PROGRESS'
    finally: w.conn.rollback()
    assert read(api_client,w.user).status_code==200


@pytest.mark.parametrize('status',['running','paused'])
def test_midnight_preserves_active_session_and_its_original_task(api_client,progress_world,real_clock,status):
    w=progress_world;unit=w.unit(1,100);task=w.task((unit,1,10))
    at=datetime(2026,11,9,15,10,tzinfo=timezone.utc);real_clock[0]=at
    session=w.conn.execute(select(t.study_sessions).where(t.study_sessions.c.user_id==w.user,
        t.study_sessions.c.status=='running')).mappings().one()
    start=at-timedelta(minutes=20)
    w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==session['id']).values(
        task_id=task,status=status,started_at=start,study_date=w.day,expected_seconds=3600,
        check_prompted_at=None,last_confirmed_elapsed_seconds=0,
        paused_at=start+timedelta(minutes=5) if status=='paused' else None))
    w.conn.execute(update(t.study_session_segments).where(t.study_session_segments.c.session_id==session['id']).values(
        started_at=start,ended_at=start+timedelta(minutes=5) if status=='paused' else None))
    ordinary(w)
    result=read(api_client,w.user,'/api/study/sessions/active')
    assert result.status_code==200,result.text
    data=result.json()['data']
    assert data['id']==session['id'] and data['taskId']==task
    assert data['status']==('active' if status=='running' else 'paused')
    assert data['todayMinutes']==(10 if status=='running' else 0)
    assert data['minutes']==(20 if status=='running' else 5)
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==task))=='planned'
    assert w.conn.scalar(select(func.count()).select_from(t.study_tasks).where(t.study_tasks.c.origin_task_id==task))==0
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==w.day
    assert w.conn.scalar(select(func.count()).select_from(t.daily_plan_totals).where(
        t.daily_plan_totals.c.user_id==w.user,t.daily_plan_totals.c.study_date==w.day))>=1


def test_expired_focus_uses_original_timeout_before_settlement(api_client,progress_world,real_clock):
    w=progress_world;unit=w.unit(1,100);task=w.task((unit,1,10))
    at=datetime(2026,11,9,15,10,tzinfo=timezone.utc);real_clock[0]=at
    session=w.conn.execute(select(t.study_sessions).where(t.study_sessions.c.user_id==w.user,
        t.study_sessions.c.status=='running')).mappings().one()
    start=at-timedelta(minutes=90)
    w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==session['id']).values(
        task_id=task,status='running',started_at=start,study_date=w.day,expected_seconds=600,
        check_prompted_at=None,last_confirmed_elapsed_seconds=0))
    w.conn.execute(update(t.study_session_segments).where(t.study_session_segments.c.session_id==session['id']).values(
        started_at=start,ended_at=None))
    ordinary(w)
    response=read(api_client,w.user)
    assert response.status_code==200,response.text
    saved=w.conn.execute(select(t.study_sessions).where(t.study_sessions.c.id==session['id'])).mappings().one()
    assert saved['status']=='auto_stopped' and saved['excluded_from_calibration']
    assert saved['measured_seconds']==1200
    ended=saved['ended_at'];ended=ended if ended.tzinfo else ended.replace(tzinfo=timezone.utc)
    assert ended==start+timedelta(minutes=20)


def test_exam_stays_active_on_exam_day_and_archives_next_day(api_client,progress_world,end_focus,real_clock):
    w=progress_world;unit=w.unit(1,100);w.task((unit,1,10))
    w.conn.execute(update(t.subjects).where(t.subjects.c.user_id==w.user).values(exam_date=date(2026,11,10)))
    w.conn.commit();end_focus(w.user);ordinary(w)
    real_clock[0]=datetime(2026,11,9,15,tzinfo=timezone.utc)
    assert read(api_client,w.user).status_code==200
    assert w.conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==w.subject['id']))=='active'
    assert not w.conn.execute(select(t.study_tasks.c.id).where(t.study_tasks.c.subject_id==w.subject['id'],
        t.study_tasks.c.scheduled_date>=date(2026,11,10),t.study_tasks.c.status.in_(['planned','partial']))).first()
    records=w.conn.execute(select(t.study_records)).all();w.conn.commit()
    real_clock[0]=datetime(2026,11,10,15,tzinfo=timezone.utc)
    assert read(api_client,w.user).status_code==200
    saved=w.conn.execute(select(t.subjects).where(t.subjects.c.id==w.subject['id'])).mappings().one()
    assert saved['status']=='archived' and saved['archive_reason']=='exam_finished'
    assert w.conn.execute(select(t.study_records)).all()==records


@pytest.mark.parametrize('last,now',[(date(2026,12,30),datetime(2026,12,31,15,tzinfo=timezone.utc)),
    (date(2028,2,27),datetime(2028,2,29,15,tzinfo=timezone.utc))])
def test_month_year_and_leap_day_catchup(api_client,progress_world,end_focus,real_clock,monkeypatch,last,now):
    w=progress_world;w.conn.commit();end_focus(w.user);ordinary(w,last=last)
    real_clock[0]=now
    seen=[];close=Planner.close_day
    def traced(self,day,**kwargs):
        seen.append(day);return close(self,day,**kwargs)
    monkeypatch.setattr(Planner,'close_day',traced)
    response=read(api_client,w.user)
    assert response.status_code==200,response.text
    expected=[];day=last+timedelta(days=1)
    today=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Seoul')).date()
    while day<today: expected.append(day);day+=timedelta(days=1)
    assert seen==expected
    assert w.conn.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==w.user))==today-timedelta(days=1)


def test_two_simultaneous_arrivals_close_each_day_only_once(api_client,progress_world,end_focus,real_clock,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    w=progress_world;w.conn.commit();end_focus(w.user);ordinary(w)
    if w.conn.dialect.name=='sqlite':
        from app.db import session
        with session.engine.connect() as conn: conn.exec_driver_sql('PRAGMA busy_timeout=20')
    entered,release=Event(),Event();seen=[];close=Planner.close_day
    def blocked(self,day,**kwargs):
        seen.append(day)
        if len(seen)==1:
            entered.set();assert release.wait(10)
        return close(self,day,**kwargs)
    monkeypatch.setattr(Planner,'close_day',blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(read,api_client,w.user)
        assert entered.wait(10)
        try:
            second=read(api_client,w.user)
            assert second.status_code==409,second.text
        finally:release.set()
        response=first.result(timeout=15)
    assert response.status_code==200,response.text
    assert seen==[date(2026,11,9),date(2026,11,10)]
    assert read(api_client,w.user).status_code==200
    assert seen==[date(2026,11,9),date(2026,11,10)]


def test_settlement_commits_even_when_requested_read_has_no_plan(api_client,domain_connection,real_clock,monkeypatch):
    from app.routers import session
    monkeypatch.setattr(session,'DEMO_MODE',False)
    user=api_client.post('/api/session').json()['data']['anonymousId']
    response=read(api_client,user,'/api/today')
    assert response.status_code==404 and response.json()['error']['code']=='NO_PLAN'
    assert domain_connection.scalar(select(t.users.c.last_assessed_date).where(t.users.c.id==user))==date(2026,11,10)


def test_missing_marker_bootstraps_from_first_owned_plan_day(api_client,progress_world,end_focus,real_clock,monkeypatch):
    w=progress_world
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.plan_id==w.plan['id']).values(scheduled_date=w.day))
    w.conn.execute(update(t.plans).where(t.plans.c.id==w.plan['id']).values(created_at=w.now,approved_at=w.now))
    w.conn.execute(update(t.users).where(t.users.c.id==w.user).values(created_at=w.now))
    w.conn.commit();end_focus(w.user);ordinary(w,last=None)
    seen=[];close=Planner.close_day
    def traced(self,day,**kwargs):
        seen.append(day);return close(self,day,**kwargs)
    monkeypatch.setattr(Planner,'close_day',traced)
    response=read(api_client,w.user)
    assert response.status_code==200,response.text
    assert seen==[date(2026,11,9),date(2026,11,10)]


def test_settlement_keeps_other_owner_and_future_marker_unchanged(api_client,progress_world,end_focus,real_clock):
    w=progress_world;w.conn.commit();end_focus(w.user);ordinary(w)
    other=api_client.post('/api/session').json()['data']['anonymousId']
    w.conn.execute(update(t.users).where(t.users.c.id==other).values(demo_date=None,last_assessed_date=date(2026,11,20)))
    before=dict(w.conn.execute(select(t.users).where(t.users.c.id==other)).mappings().one())
    subjects=w.conn.execute(select(t.subjects).where(t.subjects.c.user_id==other)).all();w.conn.commit()
    assert read(api_client,w.user).status_code==200
    assert dict(w.conn.execute(select(t.users).where(t.users.c.id==other)).mappings().one())==before
    assert w.conn.execute(select(t.subjects).where(t.subjects.c.user_id==other)).all()==subjects
    w.conn.commit()
    assert read(api_client,other).status_code==200
    assert dict(w.conn.execute(select(t.users).where(t.users.c.id==other)).mappings().one())==before
