from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.db import schema as t


def create_quiz(conn, user_id, subject_id, question_ids):
    quiz_id = str(uuid4())
    conn.execute(t.quiz_sessions.insert().values(
        id=quiz_id, user_id=user_id, subject_id=subject_id, type="practice",
        quiz_date=date(2026, 11, 9), status="ready", selected_difficulty="low",
        created_at=datetime.now(timezone.utc)))
    items = [{"id": str(uuid4()), "quiz_session_id": quiz_id, "question_id": question_id,
              "sort_order": position, "inclusion_reason": "practice"}
             for position, question_id in enumerate(question_ids, 1)]
    conn.execute(t.quiz_items.insert(), items)
    return quiz_id, items


@pytest.fixture
def quiz_case(demo_user, domain_connection):
    conn = domain_connection
    unit = conn.execute(select(t.units, t.materials.c.subject_id)
                        .join(t.materials, t.units.c.material_id == t.materials.c.id)
                        .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
                        .where(t.subjects.c.user_id == demo_user, t.subjects.c.code == "AI")
                        .order_by(t.units.c.sort_order)).mappings().first()
    concept_id = conn.scalar(select(t.concepts.c.id).where(t.concepts.c.unit_id == unit["id"]))
    questions = []
    for kind in ("choice", "ox", "short", "essay"):
        question = {
            "id": str(uuid4()), "unit_id": unit["id"], "concept_id": concept_id, "code": f"identity-{kind}",
            "type": kind, "difficulty": "low", "prompt": f"{kind} 문항",
            "choices": ([{"text": "정답"}, {"text": "오답"}] if kind == "choice" else
                        [{"text": "O"}, {"text": "X"}] if kind == "ox" else None),
            "correct_answer": "O" if kind == "ox" else "정답", "explanation": "채점 후 해설",
            "created_at": datetime.now(timezone.utc),
        }
        conn.execute(t.questions.insert().values(**question))
        conn.execute(t.question_sources.insert().values(
            id=str(uuid4()), question_id=question["id"], material_id=unit["material_id"],
            start_page=unit["start_page"], end_page=unit["start_page"], quote="정답의 원문 근거"))
        questions.append(question)
    quiz_id, items = create_quiz(conn, demo_user, unit["subject_id"], [q["id"] for q in questions])
    conn.commit()
    answers = [{"questionId": item["id"], "confidence": "sure",
                **({"choice": 0} if q["choices"] else {"text": "" if q["type"] == "essay" else "정답"})}
               for item, q in zip(items, questions)]
    return {"quiz_id": quiz_id, "subject_id": unit["subject_id"], "unit": unit,
            "items": items, "questions": questions, "answers": answers,
            "headers": {"X-Anonymous-Id": demo_user}, "user_id": demo_user}


def test_public_question_ids_are_quiz_items_through_submission(api_client, quiz_case, domain_connection):
    case = quiz_case
    response = api_client.get(f"/api/quiz/{case['quiz_id']}", headers=case["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["quizId"] == case["quiz_id"]
    questions = response.json()["data"]["questions"]
    item_ids = [item["id"] for item in case["items"]]
    assert [q["id"] for q in questions] == item_ids
    assert set(item_ids).isdisjoint(q["id"] for q in case["questions"])
    submitted = api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"],
                                json={"quizId": case["quiz_id"], "answers": case["answers"]})
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()["data"]
    assert [q["questionId"] for q in result["items"]] == item_ids
    assert result["summary"] == {"total": 4, "correct": 3, "sureCorrect": 3, "unsureCorrect": 0, "wrong": 1}
    assert api_client.get(f"/api/quiz/{case['quiz_id']}/result", headers=case["headers"]).json()["data"] == result
    assert set(domain_connection.scalars(select(t.quiz_attempts.c.quiz_item_id)
                                        .where(t.quiz_attempts.c.quiz_item_id.in_(item_ids)))) == set(item_ids)
    assert domain_connection.scalar(select(t.weak_questions.c.question_id)
                                    .where(t.weak_questions.c.question_id == case["questions"][-1]["id"]))


def test_repeated_original_question_has_distinct_public_ids(api_client, quiz_case, domain_connection):
    case = quiz_case
    repeated_id, repeated_items = create_quiz(
        domain_connection, case["user_id"], case["subject_id"], [q["id"] for q in case["questions"]])
    domain_connection.commit()
    original = api_client.get(f"/api/quiz/{case['quiz_id']}", headers=case["headers"]).json()["data"]
    repeated = api_client.get(f"/api/quiz/{repeated_id}", headers=case["headers"]).json()["data"]
    assert [q["id"] for q in repeated["questions"]] == [item["id"] for item in repeated_items]
    assert {q["id"] for q in original["questions"]}.isdisjoint(q["id"] for q in repeated["questions"])
    assert [q["prompt"] for q in original["questions"]] == [q["prompt"] for q in repeated["questions"]]


def test_question_set_keeps_solutions_private_even_after_submission(api_client, quiz_case):
    case = quiz_case
    url = f"/api/quiz/{case['quiz_id']}"
    forbidden = {"answer", "correct_answer", "correct", "is_correct", "explanation", "page", "source",
                 "evidence", "quote", "feedback", "score", "essay_score", "grading_status", "gradingStatus"}
    for submitted in (False, True):
        if submitted:
            result = api_client.post(url + "/submit", headers=case["headers"],
                                     json={"quizId": case["quiz_id"], "answers": case["answers"]})
            assert result.status_code == 200, result.text
            for item in result.json()["data"]["items"]:
                assert item["answer"] and item["explanation"] == "채점 후 해설"
                assert item["evidence"]["page"] == case["unit"]["start_page"]
                assert item["evidence"]["quote"] == "정답의 원문 근거"
        response = api_client.get(url, headers=case["headers"])
        assert response.status_code == 200, response.text
        assert forbidden.isdisjoint(response.json()["data"])
        assert all(forbidden.isdisjoint(question) for question in response.json()["data"]["questions"])
        assert "정답의 원문 근거" not in response.text and "채점 후 해설" not in response.text


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_quiz_id_selects_exact_set_under_subject_path(api_client, quiz_case):
    case = quiz_case
    response = api_client.get(f"/api/quiz/{case['subject_id']}", headers=case["headers"],
                              params={"quizId": case["quiz_id"]})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["quizId"] == case["quiz_id"]
    assert [q["id"] for q in response.json()["data"]["questions"]] == [x["id"] for x in case["items"]]
    result = api_client.post(f"/api/quiz/{case['subject_id']}/submit", headers=case["headers"],
                             json={"quizId": case["quiz_id"], "answers": case["answers"]})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["quizId"] == case["quiz_id"]


@pytest.mark.parametrize("quiz_id", [None, "invalid-uuid", 123, True, ""])
def test_submit_requires_valid_quiz_id(api_client, quiz_case, quiz_id):
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_id, "answers": quiz_case["answers"]})
    assert_error(response, 422, "INVALID_INPUT")


def test_missing_quiz_id_does_not_create_or_select_a_quiz(api_client, quiz_case, domain_connection):
    case = quiz_case
    before = domain_connection.scalar(select(func.count()).select_from(t.quiz_sessions))
    domain_connection.commit()
    response = api_client.post(f"/api/quiz/{case['subject_id']}/submit", headers=case["headers"],
                               json={"answers": case["answers"]})
    assert_error(response, 422, "INVALID_INPUT")
    response = api_client.post(f"/api/quiz/{case['subject_id']}/submit", headers=case["headers"],
                               json={"quizId": str(uuid4()), "answers": case["answers"]})
    assert_error(response, 404, "NO_QUIZ")
    assert domain_connection.scalar(select(func.count()).select_from(t.quiz_sessions)) == before


@pytest.mark.parametrize("method,path,params", [
    ("get", "/api/quiz/not-a-uuid", {}),
    ("get", "/api/quiz/not-a-uuid/result", {}),
    ("post", "/api/quiz/not-a-uuid/submit", {}),
    ("get", "/api/quiz/{quiz_id}", {"quizId": "not-a-uuid"}),
    ("get", "/api/quiz/{quiz_id}", {"quizId": ""}),
])
def test_malformed_quiz_identifiers_return_validation_error(api_client, quiz_case, method, path, params):
    kwargs = {"headers": quiz_case["headers"], "params": params}
    if method == "post":
        kwargs["json"] = {"quizId": quiz_case["quiz_id"], "answers": quiz_case["answers"]}
    response = api_client.request(method, path.format(quiz_id=quiz_case["quiz_id"]), **kwargs)
    assert_error(response, 422, "INVALID_INPUT")


@pytest.mark.parametrize("target", ["different_subject", "different_set", "unknown"])
def test_path_and_quiz_id_must_match(api_client, quiz_case, domain_connection, target):
    case = quiz_case
    if target == "different_subject":
        key = domain_connection.scalar(select(t.subjects.c.id).where(
            t.subjects.c.user_id == case["user_id"], t.subjects.c.id != case["subject_id"]))
    elif target == "different_set":
        key, _ = create_quiz(domain_connection, case["user_id"], case["subject_id"],
                             [q["id"] for q in case["questions"]])
    else:
        key = str(uuid4())
    domain_connection.commit()
    assert_error(api_client.get(f"/api/quiz/{key}", headers=case["headers"],
                               params={"quizId": case["quiz_id"]}), 404, "NO_QUIZ")
    assert_error(api_client.post(f"/api/quiz/{key}/submit", headers=case["headers"],
                                json={"quizId": case["quiz_id"], "answers": case["answers"]}), 404, "NO_QUIZ")


def test_other_users_quiz_cannot_be_selected(api_client, quiz_case, domain_connection):
    case = quiz_case
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    other_quiz = domain_connection.scalar(select(t.quiz_sessions.c.id)
                                          .where(t.quiz_sessions.c.user_id == other))
    domain_connection.commit()
    for key in (case["subject_id"], other_quiz):
        assert_error(api_client.get(f"/api/quiz/{key}", headers=case["headers"],
                                   params={"quizId": other_quiz}), 404, "NO_QUIZ")
        assert_error(api_client.post(f"/api/quiz/{key}/submit", headers=case["headers"],
                                    json={"quizId": other_quiz, "answers": case["answers"]}), 404, "NO_QUIZ")


def test_quiz_parent_subject_must_belong_to_user(api_client, quiz_case, domain_connection):
    case = quiz_case
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    foreign_subject = domain_connection.scalar(select(t.subjects.c.id).where(t.subjects.c.user_id == other))
    domain_connection.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz_id"])
                              .values(subject_id=foreign_subject))
    domain_connection.commit()
    assert_error(api_client.get(f"/api/quiz/{case['quiz_id']}", headers=case["headers"]), 404, "NO_QUIZ")
    assert_error(api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"],
                                json={"quizId": case["quiz_id"], "answers": case["answers"]}), 404, "NO_QUIZ")


def test_quiz_uuid_spellings_are_normalized(api_client, quiz_case):
    case = quiz_case
    response = api_client.get(f"/api/quiz/{case['subject_id'].upper()}", headers=case["headers"],
                              params={"quizId": case["quiz_id"].replace("-", "")})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["quizId"] == case["quiz_id"]
    response = api_client.post(f"/api/quiz/{case['quiz_id'].replace('-', '')}/submit", headers=case["headers"],
                               json={"quizId": case["quiz_id"].upper(), "answers": case["answers"]})
    assert response.status_code == 200, response.text
