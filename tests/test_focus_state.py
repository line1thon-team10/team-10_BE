from datetime import datetime, timedelta, timezone, date
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.db import schema as t
from app.seed import loader
from app.services import focus


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2030,1,2,3,tzinfo=timezone.utc)]
    monkeypatch.setattr(focus,"now",lambda: value[0])
    monkeypatch.setattr(loader,"now_utc",lambda: value[0])
    return value


def headers(user):
    return {"X-Anonymous-Id":user}


def active(client,user):
    response = client.get("/api/study/sessions/active",headers=headers(user))
    assert response.status_code == 200,response.text
    return response.json()["data"]


def action(client,user,sid,verb):
    return client.post(f"/api/study/sessions/{sid}/{verb}",headers=headers(user))


def test_pause_resume_retries_preserve_segments_and_check_baseline(api_client,clock,demo_user,domain_connection):
    sid = active(api_client,demo_user)["id"]
    for _ in range(2):
        assert action(api_client,demo_user,sid,"pause").json()["data"]["status"] == "paused"
    clock[0] += timedelta(minutes=10)
    paused = active(api_client,demo_user)
    assert paused["minutes"] == 42 and paused["pausedMinutes"] == 10
    for _ in range(2):
        assert action(api_client,demo_user,sid,"resume").status_code == 200
    assert domain_connection.scalar(select(func.count()).select_from(t.study_session_segments)) == 2
    assert domain_connection.scalar(select(t.study_sessions.c.last_confirmed_elapsed_seconds)) == 0
    domain_connection.commit()
    clock[0] += timedelta(minutes=8)
    assert active(api_client,demo_user)["checkIn"] is True
    result = action(api_client,demo_user,sid,"resume").json()["data"]
    assert result["checkIn"] is False
    assert domain_connection.scalar(select(t.study_sessions.c.last_confirmed_elapsed_seconds)) == 3000


def test_checkin_pause_acknowledges_but_retry_does_not_extend(api_client,clock,demo_user,domain_connection):
    sid = active(api_client,demo_user)["id"]
    clock[0] += timedelta(minutes=9)
    assert action(api_client,demo_user,sid,"pause").status_code == 200
    assert domain_connection.scalar(select(t.study_sessions.c.last_confirmed_elapsed_seconds)) == 3060
    domain_connection.commit()
    clock[0] += timedelta(minutes=100)
    assert action(api_client,demo_user,sid,"pause").status_code == 200
    assert domain_connection.scalar(select(t.study_sessions.c.last_confirmed_elapsed_seconds)) == 3060
    domain_connection.commit()
    assert action(api_client,demo_user,sid,"resume").status_code == 200
    clock[0] += timedelta(minutes=50)
    assert active(api_client,demo_user)["checkIn"] is True


def test_expected_time_and_quote_do_not_drift_with_task_edits(api_client,clock,demo_user,domain_connection):
    before = active(api_client,demo_user)
    domain_connection.execute(update(t.study_tasks).where(t.study_tasks.c.id==before["taskId"]).values(estimated_minutes=60))
    domain_connection.commit()
    clock[0] += timedelta(minutes=18)
    result = api_client.get(f"/api/study/sessions/{before['id']}",headers=headers(demo_user)).json()["data"]
    assert result["minutes"] == 50 and result["autoStopped"] is True
    assert result["quote"] == before["quote"]
    assert active(api_client,demo_user) is None
    assert action(api_client,demo_user,before["id"],"resume").status_code == 409
    for _ in range(2):
        assert action(api_client,demo_user,before["id"],"end").json()["data"]["minutes"] == 50


@pytest.mark.parametrize("change",["past","future","archived","foreign_plan","completed","excluded"])
def test_start_rejects_ineligible_tasks(api_client,clock,demo_user,end_focus,domain_connection,change):
    row = active(api_client,demo_user)
    end_focus(demo_user)
    conn = domain_connection
    if change in ("past","future"):
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==row["taskId"]).values(
            scheduled_date=date(2026,11,8 if change=="past" else 10)))
    elif change == "archived":
        conn.execute(update(t.subjects).where(t.subjects.c.id==row["subjectId"]).values(status="archived"))
    elif change == "foreign_plan":
        from app.domain.user import User
        from app.repositories.users import UserRepository
        other = User.anonymous(now=clock[0])
        UserRepository(conn).add(other)
        conn.execute(update(t.plans).where(t.plans.c.user_id==demo_user).values(user_id=other.id))
    else:
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==row["taskId"]).values(status=change))
    conn.commit()
    response = api_client.post("/api/study/sessions",headers=headers(demo_user),json={"taskId":row["taskId"]})
    assert response.status_code == (409 if change in ("completed","excluded") else 404)
    assert conn.scalar(select(func.count()).select_from(t.study_sessions)) == 1


def test_expired_session_does_not_block_new_start(api_client,clock,demo_user,domain_connection):
    before = active(api_client,demo_user)
    clock[0] += timedelta(hours=5)
    response = api_client.post("/api/study/sessions",headers=headers(demo_user),json={"taskId":before["taskId"]})
    assert response.status_code == 200,response.text
    after = response.json()["data"]
    assert after["id"] != before["id"] and after["minutes"] == 0
    assert domain_connection.scalar(select(t.study_sessions.c.measured_seconds).where(t.study_sessions.c.id==before["id"])) == 3000


def test_end_floor_and_backward_clock_never_write_negative_interval(api_client,clock,demo_user,domain_connection):
    before = active(api_client,demo_user)
    clock[0] += timedelta(seconds=59,microseconds=999999)
    result = action(api_client,demo_user,before["id"],"end").json()["data"]
    assert result["minutes"] == 42
    assert domain_connection.scalar(select(t.study_sessions.c.measured_seconds)) == 2579
    domain_connection.commit()
    response = api_client.post("/api/study/sessions",headers=headers(demo_user),json={"taskId":before["taskId"]})
    sid = response.json()["data"]["id"]
    clock[0] -= timedelta(hours=1)
    response = action(api_client,demo_user,sid,"end")
    assert response.status_code == 200 and response.json()["data"]["minutes"] == 0
    segment = domain_connection.execute(select(t.study_session_segments).where(t.study_session_segments.c.session_id==sid)).mappings().one()
    assert segment["ended_at"] >= segment["started_at"]
