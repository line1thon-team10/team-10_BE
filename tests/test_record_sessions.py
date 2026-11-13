from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from app.db import schema as t
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


