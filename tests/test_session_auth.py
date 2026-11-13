from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.db.schema import study_sessions, study_tasks, subjects
from app.repositories.users import UserRepository
from app.routers import session


@pytest.mark.parametrize("headers,code", [
    ({}, "NO_SESSION"),
    ({"X-Anonymous-Id": ""}, "NO_SESSION"),
    ({"X-Anonymous-Id": " "}, "UNKNOWN_SESSION"),
    ({"X-Anonymous-Id": "mock-12345678"}, "UNKNOWN_SESSION"),
    ({"X-Anonymous-Id": "null"}, "UNKNOWN_SESSION"),
    ({"X-Anonymous-Id": "g" * 32}, "UNKNOWN_SESSION"),
    ({"X-Anonymous-Id": "' OR 1=1 --"}, "UNKNOWN_SESSION"),
    ({"X-Anonymous-Id": "x" * 1000}, "UNKNOWN_SESSION"),
])
def test_invalid_credentials_fail_before_repository_lookup(api_client, monkeypatch, headers, code):
    def unexpected_lookup(*args):
        pytest.fail("Malformed credentials must never reach a UUID column")
    monkeypatch.setattr(UserRepository, "get", unexpected_lookup)
    response = api_client.get("/api/demo/state", headers=headers)
    assert response.status_code == 401
    assert response.json()["success"] is False
    assert response.json()["error"]["code"] == code


def test_unknown_uuid_is_unauthorized_and_not_echoed(api_client):
    unknown = str(uuid4())
    response = api_client.get("/api/demo/state", headers={"X-Anonymous-Id": unknown})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNKNOWN_SESSION"
    assert unknown not in response.text


@pytest.mark.parametrize("form", ["canonical", "uppercase", "hex"])
def test_valid_uuid_is_normalized_before_lookup(api_client, monkeypatch, form):
    monkeypatch.setattr(session, "DEMO_MODE", False)
    user_id = api_client.post("/api/session").json()["data"]["anonymousId"]
    token = {"canonical": user_id, "uppercase": user_id.upper(), "hex": UUID(user_id).hex}[form]
    response = api_client.get("/api/demo/state", headers={"X-Anonymous-Id": token})
    assert response.status_code == 200
    assert response.json()["data"]["hasPlan"] is False
    assert user_id not in response.text


@pytest.mark.parametrize("same_value", [True, False])
def test_duplicate_credentials_are_not_arbitrarily_selected(api_client, monkeypatch, same_value):
    monkeypatch.setattr(session, "DEMO_MODE", False)
    first = api_client.post("/api/session").json()["data"]["anonymousId"]
    second = first if same_value else api_client.post("/api/session").json()["data"]["anonymousId"]
    response = api_client.get("/api/demo/state", headers=[("X-Anonymous-Id", first), ("X-Anonymous-Id", second)])
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNKNOWN_SESSION"
    assert first not in response.text and second not in response.text


def test_request_identity_does_not_leak_between_users(api_client, monkeypatch):
    monkeypatch.setattr(session, "DEMO_MODE", True)
    first = api_client.post("/api/session").json()["data"]["anonymousId"]
    monkeypatch.setattr(session, "DEMO_MODE", False)
    second = api_client.post("/api/session").json()["data"]["anonymousId"]
    for token, has_plan in [(first, True), (second, False), (first, True)]:
        response = api_client.get("/api/demo/state", headers={"X-Anonymous-Id": token})
        assert response.status_code == 200
        assert response.json()["data"]["hasPlan"] is has_plan
    assert api_client.get("/api/demo/state").status_code == 401


def test_other_users_task_is_not_found_and_cannot_start_session(api_client, domain_connection, monkeypatch):
    monkeypatch.setattr(session, "DEMO_MODE", True)
    first = api_client.post("/api/session").json()["data"]["anonymousId"]
    monkeypatch.setattr(session, "DEMO_MODE", False)
    second = api_client.post("/api/session").json()["data"]["anonymousId"]
    task_id = domain_connection.scalar(
        select(study_tasks.c.id).join(subjects, study_tasks.c.subject_id == subjects.c.id)
        .where(subjects.c.user_id == first).limit(1)
    )
    original_sessions = set(domain_connection.scalars(select(study_sessions.c.id)))
    domain_connection.commit()
    response = api_client.post("/api/study/sessions", json={"taskId": task_id},
                               headers={"X-Anonymous-Id": second})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert set(domain_connection.scalars(select(study_sessions.c.id))) == original_sessions
    assert domain_connection.scalar(select(func.count()).select_from(study_sessions).where(
        study_sessions.c.user_id == second)) == 0
