"""Current judging FE 49c95c9 wire requests against BE, never production.

Wire shapes come from actual FE API functions captured in current_fe_wire.json.
Tests intentionally expect working old-client requests and fail on deployed 4c20454.
Copy this file and the JSON unchanged to a future immutable hotfix archive.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest


WIRE = json.loads(Path(__file__).with_name("current_fe_wire.json").read_text())


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("result", ["done", "partial", "none"])
def test_current_fe_manual_record_allows_its_null_session(
        api_client, demo_user, result):
    headers = {"X-Anonymous-Id": demo_user}
    active = _data(api_client.get("/api/study/sessions/active", headers=headers))
    _data(api_client.post(f"/api/study/sessions/{active['id']}/end", headers=headers))
    captured = next(row for row in WIRE if row["url"] == "/api/records" and row["body"]["result"] == result)
    body = deepcopy(captured["body"])
    body["taskId"] = active["taskId"]
    assert body["sessionId"] is None
    assert ("toPage" in body) is (result == "partial")
    response = api_client.post(captured["url"], headers=headers, json=body)
    assert response.status_code == 200, response.text


def test_current_fe_quiz_submit_without_new_quiz_id_field(api_client, demo_user, monkeypatch):
    from app.services.quiz import QuizService

    # The old FE can submit a deliberately empty essay. Keep it deterministic.
    original = QuizService._grade_essay
    def empty_only(self, question, text):
        assert text == "", "No external essay grading is allowed in this repro"
        return original(self, question, text)
    monkeypatch.setattr(QuizService, "_grade_essay", empty_only)

    headers = {"X-Anonymous-Id": demo_user}
    active = _data(api_client.get("/api/study/sessions/active", headers=headers))
    _data(api_client.post(f"/api/study/sessions/{active['id']}/end", headers=headers))
    _data(api_client.post("/api/records", headers=headers, json={
        "taskId": active["taskId"], "sessionId": active["id"], "result": "partial", "toPage": 13,
    }))
    quiz = _data(api_client.get(f"/api/quiz/{active['subjectId']}", headers=headers))
    captured = next(row for row in WIRE if row["url"].endswith("/submit"))
    body = deepcopy(captured["body"])
    # Substitute only actual selected item IDs and repeat the captured choice/text
    # template; no quizId is added, exactly as current FE's serializer behaves.
    templates = {"choice": body["answers"][0], "text": body["answers"][1]}
    body["answers"] = [{**deepcopy(templates["choice" if q["type"] == "choice" else "text"]),
        "questionId": q["id"]} for q in quiz["questions"]]
    assert "quizId" not in body
    response = api_client.post(f"/api/quiz/{active['subjectId']}/submit", headers=headers, json=body)
    assert response.status_code == 200, response.text
