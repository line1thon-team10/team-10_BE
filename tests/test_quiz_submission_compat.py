"""FE main49c request shape against the minimal deployed-4c compatibility fix."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_quiz_identifiers import create_quiz, quiz_case, quiz_effects


def fe_answers(answers):
    # Exact fromAnswers field shape: both unused choice/text keys are null.
    return [{"questionId": answer["questionId"], "choice": answer.get("choice"),
             "text": answer.get("text"), "confidence": answer["confidence"]} for answer in answers]


def test_current_fe_submit_without_quiz_id_preserves_result_and_no_double_scores(
        api_client, demo_user, domain_connection, end_focus):
    headers = {"X-Anonymous-Id": demo_user}
    active = api_client.get("/api/study/sessions/active", headers=headers).json()["data"]
    end_focus(demo_user)
    recorded = api_client.post("/api/records", headers=headers, json={"taskId": active["taskId"],
        "sessionId": active["id"], "result": "partial", "toPage": 13})
    assert recorded.status_code == 200, recorded.text
    opened = api_client.get(f"/api/quiz/{active['subjectId']}", headers=headers)
    assert opened.status_code == 200, opened.text
    quiz = opened.json()["data"]
    body = {"savedGrading": True, "answers": fe_answers([
        {"questionId": question["id"], "confidence": "sure",
         **({"choice": 0} if question["type"] == "choice" else {"text": ""})}
        for question in quiz["questions"]])}
    assert "quizId" not in body
    response = api_client.post(f"/api/quiz/{active['subjectId']}/submit", headers=headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["data"]["quizId"] == quiz["quizId"]
    assert [row["questionId"] for row in result["data"]["items"]] == [row["id"] for row in quiz["questions"]]
    assert result["data"]["summary"]["total"] == len(quiz["questions"])
    assert api_client.get(f"/api/quiz/{active['subjectId']}/result", headers=headers).json() == result
    conn = domain_connection
    before = (conn.execute(select(t.quiz_attempts)).all(), conn.execute(select(t.mastery_history)).all())
    conn.commit()
    repeated = api_client.post(f"/api/quiz/{active['subjectId']}/submit", headers=headers, json=body)
    assert repeated.status_code == 409 and repeated.json()["error"]["code"] == "QUIZ_FINALIZED"
    assert (conn.execute(select(t.quiz_attempts)).all(), conn.execute(select(t.mastery_history)).all()) == before


def test_legacy_direct_quiz_key_selects_only_that_owned_set(api_client, quiz_case, domain_connection):
    case, conn = quiz_case, domain_connection
    other, items = create_quiz(conn, case["user_id"], case["subject_id"], [q["id"] for q in case["questions"]])
    conn.commit()
    response = api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"],
        json={"answers": fe_answers(case["answers"]), "savedGrading": False})
    assert response.status_code == 200, response.text
    assert conn.scalar(select(t.quiz_sessions.c.status).where(t.quiz_sessions.c.id == other)) == "ready"
    assert conn.execute(select(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id.in_([row["id"] for row in items]))).first() is None


@pytest.mark.parametrize("change", ["other_set", "missing", "duplicate", "foreign_key"])
def test_legacy_body_cannot_select_other_set_or_weaken_completeness(
        api_client, quiz_case, domain_connection, change):
    case, conn = quiz_case, domain_connection
    body = {"answers": fe_answers(case["answers"]), "savedGrading": False}
    key = case["quiz_id"]
    if change == "other_set":
        _, items = create_quiz(conn, case["user_id"], case["subject_id"], [q["id"] for q in case["questions"]])
        body["answers"][0]["questionId"] = items[0]["id"]
    elif change == "missing":
        body["answers"].pop()
    elif change == "duplicate":
        body["answers"][1] = deepcopy(body["answers"][0])
    else:
        other = api_client.post("/api/session").json()["data"]["anonymousId"]
        key = conn.scalar(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id == other))
    before = quiz_effects(conn, case)
    conn.commit()
    response = api_client.post(f"/api/quiz/{key}/submit", headers=case["headers"], json=body)
    assert response.status_code == (404 if change == "foreign_key" else 422), response.text
    assert quiz_effects(conn, case) == before
