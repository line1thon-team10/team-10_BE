from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db import schema as t
from app.seed import loader
from app.services import focus


@pytest.fixture
def frozen_clock(monkeypatch):
    at = datetime(2030, 1, 2, 0, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(loader, "now_utc", lambda: at)
    monkeypatch.setattr(focus, "now", lambda: at)
    return at


def test_day9_focus_is_real_42_minutes_and_end_does_not_complete_task(api_client, frozen_clock, demo_user, domain_connection):
    headers = {"X-Anonymous-Id": demo_user}
    active = api_client.get("/api/study/sessions/active", headers=headers).json()["data"]
    assert active["status"] == "active" and active["minutes"] == 42
    assert not active["checkIn"] and not active["autoStopped"] and not active["recorded"]
    assert api_client.post("/api/demo/next-day", headers=headers).json()["error"]["code"] == "SESSION_RUNNING"
    # 같은 태스크로 시작해도 기존 시드 세션을 이어서 사용한다.
    resumed = api_client.post("/api/study/sessions", json={"taskId": active["taskId"]}, headers=headers)
    assert resumed.json()["data"]["id"] == active["id"]
    for _ in range(2):
        ended = api_client.post(f"/api/study/sessions/{active['id']}/end", headers=headers).json()["data"]
        assert ended["status"] == "ended" and ended["minutes"] == 42
    assert api_client.get("/api/study/sessions/active", headers=headers).json()["data"] is None
    conn = domain_connection
    row = conn.execute(select(t.study_sessions)).mappings().one()
    assert row["study_date"].isoformat() == "2026-11-09" and row["measured_seconds"] == 2520
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == active["taskId"])) == "planned"
    assert conn.scalar(select(func.count()).select_from(t.study_records).where(t.study_records.c.session_id == active["id"])) == 0
    segment = conn.execute(select(t.study_session_segments)).mappings().one()
    assert segment["ended_at"] - segment["started_at"] == timedelta(minutes=42)


@pytest.mark.parametrize("seed", ["empty", "overdue"])
def test_non_focus_seed_removes_session_only_for_its_owner(api_client, frozen_clock, demo_user, seed):
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    headers = {"X-Anonymous-Id": demo_user}
    assert api_client.post("/api/demo/seed", json={"seed": seed}, headers=headers).status_code == 200
    assert api_client.get("/api/study/sessions/active", headers=headers).json()["data"] is None
    assert api_client.get("/api/study/sessions/active", headers={"X-Anonymous-Id": other}).json()["data"]["minutes"] == 42


def test_reset_replaces_session_and_rebases_real_clock(api_client, frozen_clock, demo_user, monkeypatch, domain_connection):
    headers = {"X-Anonymous-Id": demo_user}
    before = api_client.get("/api/study/sessions/active", headers=headers).json()["data"]
    later = frozen_clock + timedelta(days=7)
    monkeypatch.setattr(loader, "now_utc", lambda: later)
    monkeypatch.setattr(focus, "now", lambda: later)
    for _ in range(2):
        assert api_client.post("/api/demo/reset", headers=headers).status_code == 200
        after = api_client.get("/api/study/sessions/active", headers=headers).json()["data"]
        assert after["id"] != before["id"] and after["minutes"] == 42
    assert domain_connection.scalar(select(func.count()).select_from(t.study_sessions)) == 1
    assert domain_connection.scalar(select(func.count()).select_from(t.study_session_segments)) == 1
