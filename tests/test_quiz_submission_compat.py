"""FE main49c request shape against the minimal deployed-4c compatibility fix."""
from copy import deepcopy

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_quiz_identifiers import create_quiz, quiz_case, quiz_effects


def fe_answers(answers):
    # Exact fromAnswers field shape: both unused choice/text keys are null.
    return [{"questionId": answer["questionId"], "choice": answer.get("choice"),
             "text": answer.get("text"), "confidence": answer["confidence"]} for answer in answers]


@pytest.mark.parametrize("legacy_question_ids", [False, True])
def test_current_fe_submit_without_quiz_id_preserves_result_and_rejects_serial_repeat(
        api_client, demo_user, domain_connection, end_focus, legacy_question_ids):
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
    if legacy_question_ids:
        aliases = dict(domain_connection.execute(select(t.quiz_items.c.id, t.quiz_items.c.question_id).where(
            t.quiz_items.c.quiz_session_id == quiz["quizId"])).tuples().all())
        for answer in body["answers"]:
            answer["questionId"] = aliases[answer["questionId"]]
        domain_connection.commit()
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


@pytest.mark.parametrize("explicit", [False, True])
def test_original_uuid_payload_maps_only_inside_selected_owned_quiz(
        api_client, quiz_case, domain_connection, explicit):
    case, conn = quiz_case, domain_connection
    other, other_items = create_quiz(conn, case["user_id"], case["subject_id"], [q["id"] for q in case["questions"]])
    conn.commit()
    answers = fe_answers(case["answers"])
    for answer, question in zip(answers, case["questions"]):
        answer["questionId"] = question["id"]
    body = {"answers": answers, "savedGrading": False}
    if explicit:
        body["quizId"] = case["quiz_id"]
    response = api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"], json=body)
    assert response.status_code == 200, response.text
    assert [row["questionId"] for row in response.json()["data"]["items"]] == [item["id"] for item in case["items"]]
    assert conn.execute(select(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id.in_([row["id"] for row in other_items]))).first() is None
    assert conn.scalar(select(t.quiz_sessions.c.status).where(t.quiz_sessions.c.id == other)) == "ready"


@pytest.mark.parametrize("ambiguity", ["two_daily_sets", "duplicate_alias", "overlapping_alias"])
def test_ambiguous_original_ids_are_rejected_without_scoring(api_client, quiz_case, domain_connection, ambiguity):
    case, conn = quiz_case, domain_connection
    key, answers = case["quiz_id"], fe_answers(case["answers"])
    if ambiguity == "two_daily_sets":
        duplicate, _ = create_quiz(conn, case["user_id"], case["subject_id"], [q["id"] for q in case["questions"]])
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id.in_([duplicate, case["quiz_id"]])).values(type="daily"))
        key = case["subject_id"]
        for answer, question in zip(answers, case["questions"]):
            answer["questionId"] = question["id"]
    elif ambiguity == "duplicate_alias":
        answers[1]["questionId"] = case["questions"][0]["id"]
    else:
        key, repeated = create_quiz(conn, case["user_id"], case["subject_id"], [q["id"] for q in case["questions"][:2]])
        # Distinct DB UUID namespaces can be corrupted to share an alias. Do
        # not guess which item an old-client identifier meant in that case.
        conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == repeated[1]["id"]).values(id=case["questions"][0]["id"]))
        answers = [{**answers[0], "questionId": case["questions"][0]["id"]},
                   {**answers[0], "questionId": case["questions"][1]["id"]}]
    before = (conn.execute(select(t.quiz_attempts)).all(), conn.execute(select(t.mastery_history)).all())
    conn.commit()
    response = api_client.post(f"/api/quiz/{key}/submit", headers=case["headers"], json={"answers": answers})
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_INPUT", response.text
    assert (conn.execute(select(t.quiz_attempts)).all(), conn.execute(select(t.mastery_history)).all()) == before


def test_original_alias_cannot_admit_foreign_question_in_owned_session(api_client, quiz_case, domain_connection):
    case, conn = quiz_case, domain_connection
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    foreign = conn.scalar(select(t.questions.c.id).join(t.units, t.questions.c.unit_id == t.units.c.id)
        .join(t.materials, t.units.c.material_id == t.materials.c.id)
        .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id).where(t.subjects.c.user_id == other))
    conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == case["items"][0]["id"]).values(question_id=foreign))
    answers = fe_answers(case["answers"])
    answers[0]["questionId"] = foreign
    before = conn.execute(select(t.quiz_attempts)).all()
    conn.commit()
    response = api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"], json={"answers": answers})
    assert response.status_code == 404 and response.json()["error"]["code"] == "NO_QUIZ"
    assert conn.execute(select(t.quiz_attempts)).all() == before
