"""Cross-cutting HTTP boundaries for registered routes, plus ready-domain probes.

Domain tests still own valid response fields and business transitions. New routes
automatically enter the anonymous/authentication sweep; object fixtures are added
here as their implementations become ready.
"""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import event, func, select

from app.db import schema as t
from scripts.check_api_spec import PUBLIC_API, runtime_inventory


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["message"], str) and payload["error"]["message"]
    assert response.headers["cache-control"] == "no-store"


def test_all_registered_contract_routes_reject_missing_or_unknown_session_without_writes(api_client, domain_connection):
    from app.db import session
    from app.main import app
    inventory = runtime_inventory(app)
    methods = inventory["required_registered"] + inventory["optional_registered"]
    observed_tables = tuple(t.metadata.sorted_tables)
    before = [domain_connection.scalar(select(func.count()).select_from(table)) for table in observed_tables]
    statements = []

    def observe(_conn, _cursor, statement, _parameters, context, _executemany):
        if context.isinsert or context.isupdate or context.isdelete:
            statements.append(statement)
        elif statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")):
            statements.append(statement)

    event.listen(session.engine, "before_cursor_execute", observe)
    try:
        for method, template in methods:
            if (method, template) in PUBLIC_API:
                continue
            url = template.replace(":param", str(uuid4()))
            for headers, code in [({}, "NO_SESSION"), ({"X-Anonymous-Id": "invalid"}, "UNKNOWN_SESSION")]:
                response = api_client.request(method, url, headers=headers, json={})
                assert_error(response, 401, code)
    finally:
        event.remove(session.engine, "before_cursor_execute", observe)
    after = [domain_connection.scalar(select(func.count()).select_from(table)) for table in observed_tables]
    assert after == before
    assert statements == []


def test_ready_domains_reject_another_users_objects_before_mutation(api_client, demo_user, domain_connection):
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    conn = domain_connection
    foreign_quiz = conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.user_id == other)).mappings().first()
    foreign_subject = foreign_quiz["subject_id"]
    foreign_session = conn.execute(select(t.study_sessions).where(t.study_sessions.c.user_id == other)).mappings().one()
    foreign_task = foreign_session["task_id"]
    draft_id, now = str(uuid4()), datetime.now(timezone.utc)
    conn.execute(t.subjects.insert().values(id=draft_id, user_id=other, code=draft_id, name="draft",
                                          color="#4F46E5", created_at=now, updated_at=now))
    conn.commit()
    headers = {"X-Anonymous-Id": demo_user}
    cases = [
        ("GET", f"/api/onboarding?subjectId={draft_id}", None, "SUBJECT_NOT_FOUND"),
        ("GET", f"/api/quiz/{foreign_subject}", None, "NO_QUIZ"),
        ("GET", f"/api/quiz/{foreign_quiz['id']}/result", None, "NO_QUIZ"),
        ("POST", f"/api/quiz/{foreign_subject}/submit", {"quizId": foreign_quiz["id"], "answers": []}, "NO_QUIZ"),
        ("POST", "/api/records", {"taskId": foreign_task, "result": "done"}, "TASK_NOT_FOUND"),
        ("PATCH", f"/api/tasks/{foreign_task}/record", {"toPage": 13}, "TASK_NOT_FOUND"),
        ("GET", f"/api/study/sessions/{foreign_session['id']}", None, "SESSION_NOT_FOUND"),
    ]
    cases.extend(("POST", f"/api/study/sessions/{foreign_session['id']}/{action}", {}, "SESSION_NOT_FOUND")
                 for action in ("pause", "resume", "end"))
    before_records = conn.scalar(select(func.count()).select_from(t.study_records))
    for method, url, body, code in cases:
        response = api_client.request(method, url, json=body, headers=headers)
        assert_error(response, 404, code)
    assert conn.scalar(select(func.count()).select_from(t.study_records)) == before_records
    assert dict(conn.execute(select(t.study_sessions).where(t.study_sessions.c.id == foreign_session["id"])).mappings().one()) == dict(foreign_session)


def test_malformed_ready_domain_inputs_return_their_contract_errors(api_client, demo_user):
    headers = {"X-Anonymous-Id": demo_user}
    for url in ("/api/quiz/not-a-uuid", "/api/study/sessions/not-a-uuid"):
        assert_error(api_client.get(url, headers=headers), 422, "INVALID_INPUT")
    # Onboarding's draft context is an opaque lookup key. It deliberately hides
    # malformed/missing/foreign contexts behind the same documented 404.
    assert_error(api_client.get("/api/onboarding?subjectId=not-a-uuid", headers=headers),
                 404, "SUBJECT_NOT_FOUND")
    response = api_client.post(f"/api/quiz/{uuid4()}/submit", headers=headers, json={
        "quizId": str(uuid4()), "answers": [{"questionId": str(uuid4()), "choice": True, "confidence": "sure"}]})
    assert_error(response, 422, "INVALID_INPUT")
