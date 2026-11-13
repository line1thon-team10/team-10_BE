from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core.responses import ApiError
from app.db.schema import subjects, users
from app.repositories.users import UserRepository
from app.routers import session as route
from app.seed.demo_data import SEED_DATES
from app.services import session as service


@pytest.mark.parametrize("empty_body", [False, True])
def test_normal_session_is_fresh_and_persists_without_seed(api_client, domain_connection, monkeypatch, empty_body):
    monkeypatch.setattr(route, "DEMO_MODE", False)
    def unexpected_seed(*args):
        pytest.fail("normal sessions must not load a demo seed")
    monkeypatch.setattr(service, "load_seed", unexpected_seed)
    responses = [api_client.post("/api/session", **({"json": {}} if empty_body else {})) for _ in range(2)]
    ids = []
    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"success", "data"} and body["success"] is True
        assert set(body["data"]) == {"anonymousId"}
        user_id = UUID(body["data"]["anonymousId"])
        assert user_id.version == 4
        user = UserRepository(domain_connection).get(user_id)
        assert user.onboarding_status == "draft" and user.demo_date is None
        ids.append(user_id)
    assert ids[0] != ids[1]
    assert domain_connection.scalar(select(func.count()).select_from(subjects)) == 0


@pytest.mark.parametrize("payload", [{"anonymousId": "chosen-by-client"}, {"demoMode": True}, [], "bad"])
def test_unknown_fields_or_non_object_body_do_not_create_user(api_client, domain_connection, payload):
    response = api_client.post("/api/session", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert domain_connection.scalar(select(func.count()).select_from(users)) == 0


def test_demo_sessions_create_isolated_day9_data(api_client, domain_connection, monkeypatch):
    monkeypatch.setattr(route, "DEMO_MODE", True)
    ids = []
    for _ in range(2):
        response = api_client.post("/api/session")
        assert response.status_code == 200, response.text
        ids.append(response.json()["data"]["anonymousId"])
    rows = domain_connection.execute(select(subjects.c.user_id, subjects.c.id)).all()
    owned = [{row.id for row in rows if row.user_id == user_id} for user_id in ids]
    assert len(owned[0]) == len(owned[1]) == 3
    assert owned[0].isdisjoint(owned[1])
    for user_id in ids:
        user = UserRepository(domain_connection).get(UUID(user_id))
        assert user.onboarding_status == "completed"
        assert user.demo_date == SEED_DATES["day9"]


@pytest.mark.parametrize("error", [RuntimeError("internal detail"), ApiError("QUIZ_SEED_MISSING", "/private/seed.json", 500)])
def test_seed_failure_rolls_back_user_and_partial_data(api_client, domain_connection, monkeypatch, error):
    monkeypatch.setattr(route, "DEMO_MODE", False)
    existing = api_client.post("/api/session").json()["data"]["anonymousId"]
    monkeypatch.setattr(route, "DEMO_MODE", True)
    real_load = service.load_seed
    def failed_load(conn, user_id, seed):
        real_load(conn, user_id, seed)
        raise error
    monkeypatch.setattr(service, "load_seed", failed_load)
    response = api_client.post("/api/session")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "SERVER_ERROR"
    assert str(error) not in response.text
    assert "/private" not in response.text
    assert domain_connection.execute(select(users.c.id)).scalars().all() == [existing]
    assert domain_connection.scalar(select(func.count()).select_from(subjects)) == 0
