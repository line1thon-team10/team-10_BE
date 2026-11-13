from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import event, func, select, update

from app.db import schema as t
from app.db import session
from app.repositories.locking import lock_user
from app.services.focus import FocusService
from tests.test_focus_state import active, action, headers


def fast_busy(conn):
    if conn.dialect.name == "sqlite":
        @event.listens_for(session.engine,"checkout")
        def limit_wait(connection,*_):
            connection.execute("PRAGMA busy_timeout=20")


@pytest.mark.parametrize("competitor",["same_start","other_start","reset","next-day"])
def test_start_serializes_with_other_focus_and_demo_requests(api_client,demo_user,end_focus,domain_connection,monkeypatch,competitor):
    task=active(api_client,demo_user)["taskId"]
    end_focus(demo_user)
    today=domain_connection.scalar(select(t.users.c.demo_date).where(t.users.c.id==demo_user))
    other=domain_connection.scalar(select(t.study_tasks.c.id).where(t.study_tasks.c.id!=task,
        t.study_tasks.c.scheduled_date==today))
    domain_connection.execute(update(t.study_tasks).where(t.study_tasks.c.id==other).values(status="planned"))
    domain_connection.commit()
    fast_busy(domain_connection)
    entered,release=Event(),Event()
    real=FocusService._active_row
    def held(self):
        entered.set()
        assert release.wait(5)
        return real(self)
    monkeypatch.setattr(FocusService,"_active_row",held)
    def start(tid):
        return api_client.post("/api/study/sessions",headers=headers(demo_user),json={"taskId":tid})
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(start,task)
        try:
            assert entered.wait(5)
            if competitor.endswith("start"):
                second=start(task if competitor=="same_start" else other)
            else:
                second=api_client.post("/api/demo/"+competitor,headers=headers(demo_user))
            assert second.status_code==409 and second.json()["error"]["code"]=="REQUEST_IN_PROGRESS"
        finally:
            release.set()
        response=first.result(timeout=5)
    assert response.status_code==200,response.text
    first_id=response.json()["data"]["id"]
    if competitor=="same_start":
        assert start(task).json()["data"]["id"]==first_id
    if competitor=="other_start":
        assert start(other).json()["error"]["code"]=="OTHER_ACTIVE"
    assert domain_connection.scalar(select(func.count()).select_from(t.study_sessions).where(
        t.study_sessions.c.user_id==demo_user,t.study_sessions.c.status.in_(["running","paused"])))==1
    assert domain_connection.scalar(select(func.count()).select_from(t.study_session_segments).where(
        t.study_session_segments.c.session_id==first_id,t.study_session_segments.c.ended_at.is_(None)))==1


def test_concurrent_resume_and_end_do_not_leave_an_open_segment(api_client,demo_user,domain_connection,monkeypatch):
    sid=active(api_client,demo_user)["id"]
    assert action(api_client,demo_user,sid,"pause").status_code==200
    fast_busy(domain_connection)
    entered,release=Event(),Event()
    real=FocusService._check
    def held(self,s):
        entered.set()
        assert release.wait(5)
        return real(self,s)
    monkeypatch.setattr(FocusService,"_check",held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(action,api_client,demo_user,sid,"resume")
        try:
            assert entered.wait(5)
            response=action(api_client,demo_user,sid,"end")
            assert response.status_code==409 and response.json()["error"]["code"]=="REQUEST_IN_PROGRESS"
        finally:
            release.set()
        assert first.result(timeout=5).status_code==200
    assert action(api_client,demo_user,sid,"end").status_code==200
    assert domain_connection.scalar(select(func.count()).select_from(t.study_session_segments).where(
        t.study_session_segments.c.session_id==sid,t.study_session_segments.c.ended_at.is_(None)))==0


def test_focus_read_respects_reset_lock_and_other_pg_users_stay_independent(api_client,demo_user,domain_connection):
    fast_busy(domain_connection)
    other=api_client.post("/api/session").json()["data"]["anonymousId"]
    lock_user(domain_connection,UUID(demo_user))
    try:
        response=api_client.get("/api/study/sessions/active",headers=headers(demo_user))
        assert response.status_code==409 and response.json()["error"]["code"]=="REQUEST_IN_PROGRESS"
        if domain_connection.dialect.name=="postgresql":
            assert api_client.get("/api/study/sessions/active",headers=headers(other)).status_code==200
    finally:
        domain_connection.rollback()


def test_start_response_failure_rolls_back_session_and_segment(api_client,demo_user,end_focus,domain_connection,monkeypatch):
    task=active(api_client,demo_user)["taskId"]
    end_focus(demo_user)
    before_sessions=set(domain_connection.scalars(select(t.study_sessions.c.id)))
    before_segments=set(domain_connection.scalars(select(t.study_session_segments.c.id)))
    domain_connection.commit()
    def fail(*_):
        raise RuntimeError("response failure after writes")
    monkeypatch.setattr(FocusService,"view",fail)
    response=api_client.post("/api/study/sessions",headers=headers(demo_user),json={"taskId":task})
    assert response.status_code==500
    assert set(domain_connection.scalars(select(t.study_sessions.c.id)))==before_sessions
    assert set(domain_connection.scalars(select(t.study_session_segments.c.id)))==before_segments
