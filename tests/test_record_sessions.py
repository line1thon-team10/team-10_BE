from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.clock import now_utc
from app.db import schema as t
from app.services import focus
from tests.test_record_transactions import headers, task_for


def test_same_ended_session_can_retry_without_duplicate_record_or_day_close(
        api_client, demo_user, end_focus, domain_connection):
    active = api_client.get("/api/study/sessions/active", headers=headers(demo_user)).json()["data"]
    end_focus(demo_user)
    body = {"taskId": active["taskId"], "sessionId": active["id"], "result": "partial", "toPage": 13}
    first = api_client.post("/api/records", headers=headers(demo_user), json=body)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["event"]["reason"] == "dayClose"
    before = task_for(domain_connection, demo_user, "AI2").record
    domain_connection.commit()
    for _ in range(2):
        response = api_client.post("/api/records", headers=headers(demo_user), json=body)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["event"]["reason"] != "dayClose"
    after = task_for(domain_connection, demo_user, "AI2")
    assert after.record["id"] == before["id"]
    assert after.record["study_date"] == before["study_date"]
    assert after.record["recorded_at"] == before["recorded_at"]
    assert after.record["session_id"] == active["id"]
    assert after.done_pages == 2
    assert domain_connection.scalar(select(func.count()).select_from(t.study_records).where(
        t.study_records.c.task_id == active["taskId"])) == 1


def test_session_attached_to_different_record_is_still_rejected(
        api_client, demo_user, end_focus, domain_connection):
    active = api_client.get("/api/study/sessions/active", headers=headers(demo_user)).json()["data"]
    end_focus(demo_user)
    task = task_for(domain_connection, demo_user, "AI2")
    for offset in (-1, 0):
        domain_connection.execute(t.study_records.insert().values(
            id=str(uuid4()), task_id=task.id, result="partial", study_date=task.row["scheduled_date"],
            recorded_at=task.row["created_at"] + timedelta(seconds=offset), updated_at=task.row["created_at"],
            session_id=active["id"] if offset == -1 else None))
    domain_connection.commit()
    response = api_client.post("/api/records", headers=headers(demo_user), json={
        "taskId": task.id, "sessionId": active["id"], "result": "partial", "toPage": 13})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SESSION_USED"


@pytest.mark.parametrize("session_fields", [{"sessionId": None}, {}], ids=["legacy-null", "omitted"])
@pytest.mark.parametrize("result,pages,rate", [("done", 27, 100), ("partial", 20, 74), ("none", 18, 67)])
def test_manual_record_accepts_current_fe_body_without_attaching_pending_focus(
        api_client, demo_user, end_focus, domain_connection, session_fields, result, pages, rate):
    active = api_client.get("/api/study/sessions/active", headers=headers(demo_user)).json()["data"]
    end_focus(demo_user)
    # FE main 49c95c9 S7 sends null for manual records; undefined toPage is omitted.
    body = {"taskId": active["taskId"], "result": result, **session_fields}
    if result == "partial":
        body["toPage"] = 13
    response = api_client.post("/api/records", headers=headers(demo_user), json=body)
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]) == {"event"}
    event = response.json()["data"]["event"]
    assert event is None if result == "none" else event["rate"] == rate
    saved = task_for(domain_connection, demo_user, "AI2")
    assert saved.record["session_id"] is None
    assert saved.done_pages == pages - 18
    domain_connection.commit()
    today = api_client.get("/api/today", headers=headers(demo_user)).json()["data"]
    assert today["progress"]["donePages"] == pages
    assert today["progress"]["totalPages"] == 27 and today["progress"]["rate"] == rate
    task = next(row for row in today["tasks"] if row["id"] == active["taskId"])
    assert task["measuredMinutes"] is None
    assert task["pendingSessionId"] == active["id"] and task["pendingMinutes"] == active["minutes"]


def test_null_or_omitted_session_keeps_explicit_zero_minute_record_link(
        api_client, demo_user, end_focus, domain_connection, monkeypatch):
    active = api_client.get("/api/study/sessions/active", headers=headers(demo_user)).json()["data"]
    end_focus(demo_user)
    at = now_utc()
    monkeypatch.setattr(focus, "now", lambda: at)
    started = api_client.post("/api/study/sessions", headers=headers(demo_user), json={"taskId": active["taskId"]})
    assert started.status_code == 200, started.text
    session_id = started.json()["data"]["id"]
    ended = api_client.post(f"/api/study/sessions/{session_id}/end", headers=headers(demo_user))
    assert ended.status_code == 200 and ended.json()["data"]["minutes"] == 0
    for session_fields in [{"sessionId": session_id}, {"sessionId": None}, {}]:
        response = api_client.post("/api/records", headers=headers(demo_user), json={
            "taskId": active["taskId"], "result": "partial", "toPage": 13, **session_fields})
        assert response.status_code == 200, response.text
        saved = task_for(domain_connection, demo_user, "AI2")
        assert saved.record["session_id"] == session_id
        domain_connection.commit()
        today = api_client.get("/api/today", headers=headers(demo_user)).json()["data"]
        task = next(row for row in today["tasks"] if row["id"] == active["taskId"])
        assert task["measuredMinutes"] == 0 and task["measuredMinutes"] is not None


