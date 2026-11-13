from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import update

from app.db import schema as t
from app.repositories.locking import lock_user
from app.routers import today
from app.services.study import StudyData
from tests.test_focus_concurrency import fast_busy


def headers(user):
    return {"X-Anonymous-Id": user}


def task_for(conn, user, code="DB2"):
    data = StudyData(conn, user)
    return next(task for task in data.tasks if data.unit_of(task) and data.unit_of(task)["code"] == code)


@pytest.mark.parametrize("method", ["post", "patch"])
def test_record_respects_user_lock(api_client, demo_user, domain_connection, method):
    conn = domain_connection
    task = task_for(conn, demo_user)
    conn.commit()
    fast_busy(conn)
    lock_user(conn, UUID(demo_user))
    try:
        if method == "post":
            response = api_client.post("/api/records", headers=headers(demo_user),
                                       json={"taskId": task.id, "result": "partial", "toPage": 13})
        else:
            response = api_client.patch(f"/api/tasks/{task.id}/record", headers=headers(demo_user),
                                        json={"toPage": 13})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    finally:
        conn.rollback()
    assert task_for(conn, demo_user).record["result"] == "completed"


@pytest.mark.parametrize("competitor", ["record", "reset", "focus"])
def test_record_write_serializes_competing_requests(
        api_client, demo_user, domain_connection, monkeypatch, competitor):
    task = task_for(domain_connection, demo_user)
    domain_connection.commit()
    fast_busy(domain_connection)
    entered, release = Event(), Event()
    real_save = today._save_record

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(today, "_save_record", held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(api_client.patch, f"/api/tasks/{task.id}/record",
                                headers=headers(demo_user), json={"toPage": 13})
        try:
            assert entered.wait(5)
            if competitor == "record":
                response = api_client.post("/api/records", headers=headers(demo_user),
                                           json={"taskId": task.id, "result": "done"})
            elif competitor == "reset":
                response = api_client.post("/api/demo/reset", headers=headers(demo_user))
            else:
                response = api_client.get("/api/study/sessions/active", headers=headers(demo_user))
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert task_for(domain_connection, demo_user).done_pages == 2


def test_record_uses_user_refreshed_under_lock(api_client, demo_user, end_focus, domain_connection, monkeypatch):
    end_focus(demo_user)
    task = task_for(domain_connection, demo_user, "AI2")
    new_day = task.row["scheduled_date"] + timedelta(days=1)
    domain_connection.commit()
    real_lock = today.lock_user

    def refreshed(conn, user_id):
        conn.execute(update(t.users).where(t.users.c.id == str(user_id)).values(demo_date=new_day))
        return real_lock(conn, user_id)

    monkeypatch.setattr(today, "lock_user", refreshed)
    response = api_client.post("/api/records", headers=headers(demo_user),
                               json={"taskId": task.id, "result": "partial", "toPage": 13})
    assert response.status_code == 200, response.text
    assert task_for(domain_connection, demo_user, "AI2").record["study_date"] == new_day
