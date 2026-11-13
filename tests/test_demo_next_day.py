from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.db.schema import study_records, study_sessions, study_tasks, subjects, users
from app.repositories.demo import DemoRepository
from app.services.planner import Planner


def auth(user):
    return {"X-Anonymous-Id": user}


def advance(client, user):
    return client.post("/api/demo/next-day", headers=auth(user))


def test_unkeyed_requests_advance_separately_and_preserve_seed(api_client, demo_user):
    for day in (10,11):
        response = advance(api_client, demo_user)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["today"] == f"2026-11-{day}" and data["seed"] == "day9"
        assert data["judgment"] in {"none","carry","replan"}


def test_empty_plan_rejected_without_date_change(api_client, demo_user):
    before = api_client.post("/api/demo/seed", json={"seed":"empty"}, headers=auth(demo_user)).json()
    response = advance(api_client,demo_user)
    assert response.status_code == 400 and response.json()["error"]["code"] == "NO_PLAN"
    assert api_client.get("/api/demo/state",headers=auth(demo_user)).json() == before


def test_last_exam_day_is_allowed_destination_but_not_departure(api_client, demo_user, domain_connection):
    conn = domain_connection
    last = conn.scalar(select(func.max(subjects.c.exam_date)).where(subjects.c.user_id == demo_user))
    conn.execute(update(users).where(users.c.id == demo_user).values(demo_date=last-timedelta(days=1)))
    conn.commit()
    response = advance(api_client,demo_user)
    assert response.status_code == 200 and response.json()["data"]["today"] == last.isoformat()
    assert response.json()["data"]["firstExam"]["examDate"] == last.isoformat()
    response = advance(api_client,demo_user)
    assert response.status_code == 400 and response.json()["error"]["code"] == "DEMO_END"
    assert conn.scalar(select(users.c.demo_date).where(users.c.id == demo_user)) == last


def test_archive_on_day_after_exam_preserves_records_and_other_user(api_client, demo_user, domain_connection):
    conn=domain_connection
    other=api_client.post("/api/session").json()["data"]["anonymousId"]
    conn.execute(update(users).where(users.c.id == demo_user).values(demo_date=date(2026,11,11)))
    conn.commit()
    assert advance(api_client,demo_user).status_code == 200
    ds=select(subjects).where(subjects.c.user_id == demo_user,subjects.c.code=="DS")
    assert conn.execute(ds).mappings().one()["status"] == "active"
    count=conn.scalar(select(func.count()).select_from(study_records))
    conn.commit()
    assert advance(api_client,demo_user).status_code == 200
    row=conn.execute(ds).mappings().one()
    assert row["status"]=="archived" and row["archive_reason"]=="exam_finished" and row["archived_at"] is not None
    assert conn.scalar(select(func.count()).select_from(study_records))==count
    assert conn.scalar(select(subjects.c.status).where(subjects.c.user_id==other,subjects.c.code=="DS"))=="active"


def test_failure_after_day_closure_rolls_back_every_write(api_client,demo_user,domain_connection,monkeypatch):
    conn=domain_connection
    before=api_client.get("/api/demo/state",headers=auth(demo_user)).json()
    task_ids=set(conn.scalars(select(study_tasks.c.id)))
    conn.commit()
    real=Planner.advance_day
    def fail(self):
        real(self)
        raise RuntimeError("abort closure")
    monkeypatch.setattr(Planner,"advance_day",fail)
    assert advance(api_client,demo_user).status_code==500
    assert api_client.get("/api/demo/state",headers=auth(demo_user)).json()==before
    assert set(conn.scalars(select(study_tasks.c.id)))==task_ids


def test_date_is_reread_after_user_lock(api_client,demo_user,domain_connection):
    # 다른 요청이 변경한 날짜를 다음 요청이 정상적으로 읽는지 확인한다.
    conn=domain_connection
    conn.execute(update(users).where(users.c.id==demo_user).values(demo_date=date(2026,11,10)))
    conn.commit()
    assert advance(api_client,demo_user).json()["data"]["today"]=="2026-11-11"


def test_next_day_conflicts_with_reset_lock(api_client,demo_user,domain_connection):
    from app.db import session
    if domain_connection.dialect.name=="sqlite":
        with session.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA busy_timeout=10")
    DemoRepository(domain_connection).lock_user(UUID(demo_user))
    try:
        response=advance(api_client,demo_user)
        assert response.status_code==409 and response.json()["error"]["code"]=="REQUEST_IN_PROGRESS"
    finally:
        domain_connection.rollback()
    assert advance(api_client,demo_user).json()["data"]["today"]=="2026-11-10"


@pytest.mark.parametrize("status", ["running","paused"])
def test_active_focus_blocks_day_closure(api_client,demo_user,domain_connection,status):
    from app.core.clock import now_utc
    conn=domain_connection
    task=conn.scalar(select(study_tasks.c.id))
    conn.execute(study_sessions.insert().values(id=str(uuid4()),user_id=demo_user,task_id=task,status=status,
                 started_at=now_utc(),created_at=now_utc(),study_date=date(2026,11,9)))
    conn.commit()
    response=advance(api_client,demo_user)
    assert response.status_code==409 and response.json()["error"]["code"]=="SESSION_RUNNING"
    assert conn.scalar(select(users.c.demo_date).where(users.c.id==demo_user))==date(2026,11,9)


@pytest.mark.parametrize("day,exam,code", [(None,date(2026,11,18),"BAD_DATE"),
    (date(2026,11,9),None,"DEMO_END"), (date.max,date.max,"DEMO_END")])
def test_missing_dates_and_maximum_date_do_not_advance(api_client,demo_user,domain_connection,day,exam,code):
    conn=domain_connection
    conn.execute(update(users).where(users.c.id==demo_user).values(demo_date=day))
    conn.execute(update(subjects).where(subjects.c.user_id==demo_user).values(exam_date=exam))
    conn.commit()
    response=advance(api_client,demo_user)
    assert response.status_code==400 and response.json()["error"]["code"]==code
    assert conn.scalar(select(users.c.demo_date).where(users.c.id==demo_user))==day
