from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.db import session
from app.db.schema import demo_day_requests, users
from app.repositories.demo import DemoRepository
from app.services.planner import Planner


def headers(user,key):
    return {"X-Anonymous-Id":user,"Idempotency-Key":str(key)}


def post(client,user,key):
    return client.post("/api/demo/next-day",headers=headers(user,key))


def test_replay_uses_durable_response_without_reexecuting_planner(api_client,demo_user,domain_connection,monkeypatch):
    key=uuid4()
    first=post(api_client,demo_user,key)
    assert first.status_code==200
    stored=domain_connection.scalar(select(demo_day_requests.c.response))
    assert stored==first.json()["data"]
    domain_connection.commit()
    session.engine.dispose()  # 다음 요청은 새 DB 연결과 새 서비스 객체로 수행한다.
    def forbidden(*args):
        pytest.fail("a completed key must not execute the planner")
    monkeypatch.setattr(Planner,"advance_day",forbidden)
    assert post(api_client,demo_user,str(key).upper()).json()==first.json()
    assert domain_connection.scalar(select(users.c.demo_date).where(users.c.id==demo_user))==date(2026,11,10)


def test_same_key_is_independent_for_different_users(api_client,demo_user,domain_connection):
    other=api_client.post("/api/session").json()["data"]["anonymousId"]
    key=uuid4()
    for user in (demo_user,other):
        response=post(api_client,user,key)
        assert response.status_code==200 and response.json()["data"]["today"]=="2026-11-10"
    assert domain_connection.scalar(select(func.count()).select_from(demo_day_requests))==2


@pytest.mark.parametrize("path,body",[("reset",None),("seed",{"seed":"empty"})])
def test_reset_preserves_old_receipt_without_advancing_new_state(api_client,demo_user,path,body):
    key=uuid4()
    first=post(api_client,demo_user,key)
    reset=api_client.post("/api/demo/"+path,json=body,headers={"X-Anonymous-Id":demo_user})
    assert reset.status_code==200
    assert post(api_client,demo_user,key).json()==first.json()
    assert api_client.get("/api/demo/state",headers={"X-Anonymous-Id":demo_user}).json()==reset.json()


def test_response_save_failure_rolls_back_day_and_allows_same_key_retry(api_client,demo_user,domain_connection,monkeypatch):
    key=uuid4()
    real=DemoRepository.save_request
    def fail(self,*args):
        real(self,*args)
        raise RuntimeError("failure after receipt insert")
    monkeypatch.setattr(DemoRepository,"save_request",fail)
    assert post(api_client,demo_user,key).status_code==500
    assert domain_connection.scalar(select(users.c.demo_date).where(users.c.id==demo_user))==date(2026,11,9)
    assert domain_connection.scalar(select(func.count()).select_from(demo_day_requests))==0
    domain_connection.commit()
    monkeypatch.setattr(DemoRepository,"save_request",real)
    response=post(api_client,demo_user,key)
    assert response.status_code==200 and response.json()["data"]["today"]=="2026-11-10"


def test_mismatching_stored_hash_is_conflict(api_client,demo_user,domain_connection):
    key=uuid4()
    assert post(api_client,demo_user,key).status_code==200
    domain_connection.execute(update(demo_day_requests).values(request_hash="different request"))
    domain_connection.commit()
    response=post(api_client,demo_user,key)
    assert response.status_code==409 and response.json()["error"]["code"]=="IDEMPOTENCY_CONFLICT"
    assert domain_connection.scalar(select(users.c.demo_date).where(users.c.id==demo_user))==date(2026,11,10)


@pytest.mark.parametrize("value",["","bad"," ","x"*1000])
def test_invalid_key_is_rejected_before_date_change(api_client,demo_user,domain_connection,value):
    response=post(api_client,demo_user,value)
    assert response.status_code==422 and response.json()["error"]["code"]=="INVALID_INPUT"
    assert domain_connection.scalar(select(users.c.demo_date).where(users.c.id==demo_user))==date(2026,11,9)


def test_duplicate_key_header_is_rejected(api_client,demo_user):
    key=str(uuid4())
    response=api_client.post("/api/demo/next-day",headers=[("X-Anonymous-Id",demo_user),
                             ("Idempotency-Key",key),("Idempotency-Key",key)])
    assert response.status_code==422


@pytest.mark.parametrize("same_key",[True,False])
def test_concurrent_requests_do_not_double_apply(api_client,demo_user,domain_connection,monkeypatch,same_key):
    if domain_connection.dialect.name=="sqlite":
        @event.listens_for(session.engine,"checkout")
        def quick_busy(connection,*args):
            connection.execute("PRAGMA busy_timeout=20")
    entered,release=Event(),Event()
    real=Planner.advance_day
    def held(self):
        entered.set()
        assert release.wait(5), "test did not release held request"
        return real(self)
    monkeypatch.setattr(Planner,"advance_day",held)
    first_key=uuid4()
    second_key=first_key if same_key else uuid4()
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future=executor.submit(post,api_client,demo_user,first_key)
        try:
            assert entered.wait(5)
            second=post(api_client,demo_user,second_key)
            assert second.status_code==409 and second.json()["error"]["code"]=="REQUEST_IN_PROGRESS"
        finally:
            release.set()
        first=first_future.result(timeout=5)
    assert first.status_code==200,first.text
    again=post(api_client,demo_user,second_key)
    assert again.status_code==200
    if same_key:
        assert again.json()==first.json()
    else:
        assert again.json()["data"]["today"]=="2026-11-11"
    expected=1 if same_key else 2
    assert domain_connection.scalar(select(func.count()).select_from(demo_day_requests))==expected


def test_last_day_success_can_be_replayed_after_end_guard(api_client,demo_user,domain_connection):
    domain_connection.execute(update(users).where(users.c.id==demo_user).values(demo_date=date(2026,11,17)))
    domain_connection.commit()
    key=uuid4()
    first=post(api_client,demo_user,key)
    assert first.status_code==200 and first.json()["data"]["today"]=="2026-11-18"
    assert post(api_client,demo_user,key).json()==first.json()
    assert post(api_client,demo_user,uuid4()).json()["error"]["code"]=="DEMO_END"


def test_postgres_user_locks_do_not_block_other_users(api_client,demo_user,domain_connection,monkeypatch):
    if domain_connection.dialect.name!="postgresql":
        pytest.skip("SQLite uses a database-wide writer lock")
    other=api_client.post("/api/session").json()["data"]["anonymousId"]
    entered,release=Event(),Event()
    real=Planner.advance_day
    def held(self):
        if self.user["id"]==demo_user:
            entered.set()
            assert release.wait(5)
        return real(self)
    monkeypatch.setattr(Planner,"advance_day",held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(post,api_client,demo_user,uuid4())
        try:
            assert entered.wait(5)
            response=post(api_client,other,uuid4())
            assert response.status_code==200,response.text
        finally:
            release.set()
        assert first.result(timeout=5).status_code==200
