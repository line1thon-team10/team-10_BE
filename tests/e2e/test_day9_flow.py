"""API-only DAY9 integration; copy into an immutable archive's tests directory.

One connected flow, parameterized by the repository's SQLite/isolated-PG fixtures.
No browser, runtime server, real credentials, provider calls, or shared DB data.
"""
from sqlalchemy import select

from app.ai.gateway import ProcessExecutor
from app.db import schema as t
from app.seed.essay_examples import DEMO_ESSAY


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _persistent_result(conn, quiz_id):
    items = select(t.quiz_items.c.id).where(t.quiz_items.c.quiz_session_id == quiz_id)
    snapshot = {}
    for name, stmt in {
        "answers": select(t.quiz_answers).where(t.quiz_answers.c.quiz_item_id.in_(items)).order_by(t.quiz_answers.c.id),
        "heads": select(t.quiz_answer_heads).where(t.quiz_answer_heads.c.quiz_item_id.in_(items)).order_by(t.quiz_answer_heads.c.quiz_item_id),
        "attempts": select(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id.in_(items)).order_by(t.quiz_attempts.c.id),
        "history": select(t.mastery_history).where(t.mastery_history.c.quiz_session_id == quiz_id).order_by(t.mastery_history.c.id),
        "submission": select(t.quiz_submission_states).where(t.quiz_submission_states.c.quiz_session_id == quiz_id),
    }.items():
        snapshot[name] = [dict(row) for row in conn.execute(stmt).mappings()]
    conn.commit()
    return snapshot


def test_day9_put_restore_saved_submit_result_and_replay(
        api_client, demo_user, domain_connection, monkeypatch):
    # Keep the real process validator, but forbid its provider branch.
    original_execute = ProcessExecutor.execute
    executions = []

    def cached_only(self, identity, *, provider, cached_json=None, **kwargs):
        assert provider is None, "This saved-only flow must not initialize a provider"
        assert cached_json is not None, "Only the approved owned seed cache is allowed"
        executions.append(identity.input_hash)
        return original_execute(self, identity, provider=None, cached_json=cached_json, **kwargs)

    monkeypatch.setattr(ProcessExecutor, "execute", cached_only)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("UPSTAGE_API_KEY", raising=False)
    headers = {"X-Anonymous-Id": demo_user}
    client, conn = api_client, domain_connection

    active = _data(client.get("/api/study/sessions/active", headers=headers))
    assert active is not None
    _data(client.post(f"/api/study/sessions/{active['id']}/end", headers=headers))
    _data(client.post("/api/records", headers=headers, json={
        "taskId": active["taskId"], "sessionId": active["id"],
        "result": "partial", "toPage": 13,
    }))
    opened = _data(client.get(f"/api/quiz/{active['subjectId']}", headers=headers))
    quiz_id = opened["quizId"]
    questions = list(conn.execute(select(t.questions, t.quiz_items.c.id.label("item_id"))
        .join(t.quiz_items, t.quiz_items.c.question_id == t.questions.c.id)
        .where(t.quiz_items.c.quiz_session_id == quiz_id)
        .order_by(t.quiz_items.c.sort_order)).mappings())
    essay = next(row for row in questions if row["type"] == "essay")
    assert essay["code"] == DEMO_ESSAY["code"]
    cache_ids = set(conn.scalars(select(t.ai_saved_results.c.id)
        .where(t.ai_saved_results.c.user_id == demo_user)))
    assert cache_ids
    conn.commit()

    expected = {}
    for row in questions:
        payload = {"confidence": "sure"}
        if row["choices"]:
            payload["choice"] = next(index for index, option in enumerate(row["choices"])
                if option["text"] == row["correct_answer"])
        else:
            payload["text"] = DEMO_ESSAY["answer"] if row["type"] == "essay" else row["correct_answer"]
        receipt = _data(client.put(f"/api/quiz/{quiz_id}/answers/{row['item_id']}",
            headers=headers, json={**payload, "revision": 0}))
        expected[row["item_id"]] = {"questionId": row["item_id"],
            "choice": payload.get("choice"), "text": payload.get("text"),
            "confidence": payload["confidence"], "revision": receipt["revision"]}

    restored = _data(client.get(f"/api/quiz/{active['subjectId']}",
        headers=headers, params={"quizId": quiz_id}))
    assert restored["quizId"] == quiz_id
    assert {row["questionId"]: row for row in restored["savedAnswers"]} == expected
    saved_state = _persistent_result(conn, quiz_id)
    # Submit only the values actually restored through HTTP, as after a reload.
    body = {"quizId": quiz_id, "savedGrading": True, "answers": [
        {key: value for key, value in answer.items() if key != "revision" and value is not None}
        for answer in restored["savedAnswers"]
    ]}
    submitted = client.post(f"/api/quiz/{active['subjectId']}/submit", headers=headers, json=body)
    result = _data(submitted)
    graded_essay = next(row for row in result["items"] if row["type"] == "essay")
    assert result["essayGrading"] == "saved" and graded_essay["score"] == 70
    assert len(executions) == 1
    first_state = _persistent_result(conn, quiz_id)
    assert first_state["answers"] == saved_state["answers"]
    assert first_state["heads"] == saved_state["heads"]
    assert len(first_state["attempts"]) == len(questions)
    assert first_state["history"]
    assert first_state["submission"][0]["result_snapshot"] == result
    job = conn.execute(select(t.quiz_grading_jobs)
        .join(t.quiz_answers, t.quiz_answers.c.id == t.quiz_grading_jobs.c.answer_id)
        .where(t.quiz_answers.c.quiz_item_id == essay["item_id"])).mappings().one()
    assert job["execution_mode"] == "saved"
    assert job["result"]["provenance"]["saved_result_id"] in cache_ids
    assert conn.execute(select(t.ai_usage_logs).where(t.ai_usage_logs.c.user_id == demo_user)).first() is None
    conn.commit()

    fetched = client.get(f"/api/quiz/{active['subjectId']}/result",
        headers=headers, params={"quizId": quiz_id})
    assert fetched.status_code == 200 and fetched.json() == submitted.json()
    replayed = client.post(f"/api/quiz/{active['subjectId']}/submit", headers=headers, json=body)
    assert replayed.status_code == 200 and replayed.json() == submitted.json()
    assert _persistent_result(conn, quiz_id) == first_state
    assert len(executions) == 1
