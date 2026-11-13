from app.routers import session
from app.services.session import SessionService


def test_success_auth_validation_and_not_found_are_not_cached(api_client, monkeypatch):
    monkeypatch.setattr(session, "DEMO_MODE", False)
    responses = [
        (api_client.post("/api/session"), 200),
        (api_client.get("/api/demo/state"), 401),
        (api_client.post("/api/session", json={"unexpected": True}), 422),
        (api_client.get("/api/missing-route"), 404),
    ]
    for response, status in responses:
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"


def test_unhandled_failure_is_not_cached_or_exposed(api_client, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("internal connection detail")
    monkeypatch.setattr(SessionService, "create", fail)
    response = api_client.post("/api/session")
    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "SERVER_ERROR"
    assert "internal connection detail" not in response.text
