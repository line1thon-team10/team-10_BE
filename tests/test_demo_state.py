from datetime import date

import pytest
from sqlalchemy import select, update

from app.db.schema import subjects, users
from app.routers import demo
from app.services import demo as service


def auth(user):
    return {"X-Anonymous-Id": user}


def state(client, user):
    response = client.get("/api/demo/state", headers=auth(user))
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("seed", ["day9", "overdue", "empty"])
def test_seed_persists_after_new_request(api_client, demo_user, seed):
    response = api_client.post("/api/demo/seed", json={"seed": seed}, headers=auth(demo_user))
    assert response.status_code == 200
    data = state(api_client, demo_user)
    assert data == response.json()["data"]
    assert data["seed"] == seed and data["judgment"] is None
    assert data["hasPlan"] is (seed != "empty")
    assert isinstance(data["seeds"], dict)
    if seed == "empty":
        assert data["firstExam"] is None


def test_first_exam_filters_past_and_archived_and_breaks_ties(api_client, demo_user, domain_connection):
    conn = domain_connection
    ids = sorted(conn.execute(select(subjects.c.id).where(subjects.c.user_id == demo_user)).scalars())
    conn.execute(update(subjects).where(subjects.c.user_id == demo_user).values(exam_date=date(2026, 11, 9)))
    conn.execute(update(subjects).where(subjects.c.id == ids[0]).values(status="archived"))
    conn.commit()
    assert state(api_client, demo_user)["firstExam"]["subjectId"] == ids[1]
    conn.execute(update(subjects).where(subjects.c.user_id == demo_user).values(exam_date=date(2026, 11, 8)))
    conn.commit()
    assert state(api_client, demo_user)["firstExam"] is None
    conn.execute(update(subjects).where(subjects.c.user_id == demo_user).values(exam_date=None))
    conn.commit()
    assert state(api_client, demo_user)["firstExam"] is None


@pytest.mark.parametrize("path,body", [("seed", {"seed": "empty"}), ("reset", None)])
def test_initialization_preserves_other_user_and_reset_default(api_client, demo_user, path, body):
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    before = api_client.get("/api/books", headers=auth(other)).json()
    response = api_client.post("/api/demo/"+path, json=body, headers=auth(demo_user))
    assert response.status_code == 200
    assert response.json()["data"]["seed"] == ("empty" if path == "seed" else "day9")
    assert response.json()["data"]["judgment"] is None
    assert api_client.get("/api/books", headers=auth(other)).json() == before


@pytest.mark.parametrize("path,body", [("seed", {"seed": "empty"}), ("reset", None), ("next-day", None)])
def test_disabled_demo_mutations_are_forbidden(api_client, demo_user, monkeypatch, path, body):
    before = state(api_client, demo_user)
    monkeypatch.setattr(demo, "DEMO_MODE", False)
    response = api_client.post("/api/demo/"+path, json=body, headers=auth(demo_user))
    assert response.status_code == 403 and response.json()["error"]["code"] == "DEMO_DISABLED"
    assert state(api_client, demo_user) == before


@pytest.mark.parametrize("body,status,code", [({"seed":"invalid"},400,"BAD_SEED"),
    ({"seed":1},422,"INVALID_INPUT"), ({"seed":"day9","extra":True},422,"INVALID_INPUT"),
    ({},422,"INVALID_INPUT"), ({"seed":None},422,"INVALID_INPUT")])
def test_invalid_seed_preserves_state(api_client, demo_user, body, status, code):
    before = state(api_client, demo_user)
    response = api_client.post("/api/demo/seed", json=body, headers=auth(demo_user))
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert state(api_client, demo_user) == before


@pytest.mark.parametrize("path", ["seed", "reset"])
def test_seed_failure_rolls_back_old_state_and_rows(api_client, demo_user, domain_connection, monkeypatch, path):
    before = state(api_client, demo_user)
    ids = set(domain_connection.scalars(select(subjects.c.id)))
    domain_connection.commit()
    real_seed = service.load_seed
    def fail(conn, user_id, seed):
        real_seed(conn, user_id, seed)
        raise RuntimeError("failed after writes")
    monkeypatch.setattr(service, "load_seed", fail)
    response = api_client.post("/api/demo/"+path, json={"seed":"overdue"} if path=="seed" else None,
                               headers=auth(demo_user))
    assert response.status_code == 500
    assert state(api_client, demo_user) == before
    assert set(domain_connection.scalars(select(subjects.c.id))) == ids


@pytest.mark.parametrize("path,body", [("seed", {"seed":"empty"}), ("reset", None)])
def test_competing_initialization_returns_conflict(api_client, demo_user, domain_connection, path, body):
    from uuid import UUID
    from app.db import session
    from app.repositories.demo import DemoRepository
    if domain_connection.dialect.name == "sqlite":
        with session.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA busy_timeout=10")
    DemoRepository(domain_connection).lock_user(UUID(demo_user))
    try:
        response = api_client.post("/api/demo/"+path, json=body, headers=auth(demo_user))
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    finally:
        domain_connection.rollback()
    assert state(api_client, demo_user)["seed"] == "day9"
