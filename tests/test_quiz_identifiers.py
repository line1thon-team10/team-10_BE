from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.db import schema as t
from app.services.quiz import QuizService


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


def test_missing_quiz_id_never_creates_or_selects_an_unrelated_set(api_client, quiz_case, domain_connection):
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
    answers = deepcopy(case["answers"])
    for answer in answers:
        answer["questionId"] = answer["questionId"].replace("-", "").upper()
    response = api_client.post(f"/api/quiz/{case['quiz_id'].replace('-', '')}/submit", headers=case["headers"],
                               json={"quizId": case["quiz_id"].upper(), "answers": answers})
    assert response.status_code == 200, response.text


def quiz_effects(conn, case):
    question_ids = [question["id"] for question in case["questions"]]
    return {
        "session": dict(conn.execute(select(t.quiz_sessions).where(
            t.quiz_sessions.c.id == case["quiz_id"])).mappings().one()),
        "attempts": conn.execute(select(t.quiz_attempts).where(
            t.quiz_attempts.c.quiz_item_id.in_([item["id"] for item in case["items"]]))).all(),
        "weak": conn.execute(select(t.weak_questions).where(
            t.weak_questions.c.question_id.in_(question_ids)).order_by(t.weak_questions.c.question_id)).all(),
        "unit": conn.execute(select(t.units).where(t.units.c.id == case["unit"]["id"])).one(),
        "history": conn.execute(select(t.mastery_history).where(
            t.mastery_history.c.quiz_session_id == case["quiz_id"])).all(),
    }


@pytest.mark.parametrize("kind", [
    "original_question", "other_set_same_question", "other_subject", "other_user", "unknown",
    "duplicate", "duplicate_normalized", "missing", "empty", "extra",
])
def test_answers_must_match_exact_item_set(api_client, quiz_case, domain_connection, kind):
    case = quiz_case
    answers = deepcopy(case["answers"])
    if kind == "original_question":
        answers[0]["questionId"] = case["questions"][0]["id"]
    elif kind == "other_set_same_question":
        _, repeated_items = create_quiz(domain_connection, case["user_id"], case["subject_id"],
                                         [q["id"] for q in case["questions"]])
        answers[0]["questionId"] = repeated_items[0]["id"]
    elif kind in ("other_subject", "other_user"):
        user = case["user_id"] if kind == "other_subject" else api_client.post(
            "/api/session").json()["data"]["anonymousId"]
        answers[0]["questionId"] = domain_connection.scalar(select(t.quiz_items.c.id)
            .join(t.quiz_sessions, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.quiz_sessions.c.user_id == user, t.quiz_sessions.c.subject_id != case["subject_id"]))
    elif kind == "unknown":
        answers[0]["questionId"] = str(uuid4())
    elif kind in ("duplicate", "duplicate_normalized"):
        answers[1]["questionId"] = answers[0]["questionId"]
        if kind == "duplicate_normalized":
            answers[1]["questionId"] = answers[1]["questionId"].replace("-", "").upper()
    elif kind == "missing":
        answers.pop()
    elif kind == "empty":
        answers = []
    elif kind == "extra":
        answers.append({"questionId": str(uuid4()), "choice": 0, "confidence": "sure"})
    before = quiz_effects(domain_connection, case)
    domain_connection.commit()
    response = api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"],
                               json={"quizId": case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")
    assert quiz_effects(domain_connection, case) == before


@pytest.mark.parametrize("value", ["not-a-uuid", "", None, 123, True, {}, []])
def test_question_id_must_be_a_uuid(api_client, quiz_case, value):
    answers = deepcopy(quiz_case["answers"])
    answers[0]["questionId"] = value
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")


@pytest.mark.parametrize("index", [0, 1], ids=["choice", "ox"])
@pytest.mark.parametrize("value", [-1, 2, 1000000, 0.0, "0", True, False, None, [], {}])
def test_choice_requires_in_range_strict_integer(api_client, quiz_case, index, value):
    answers = deepcopy(quiz_case["answers"])
    answers[index]["choice"] = value
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")


@pytest.mark.parametrize("index", [2, 3], ids=["short", "essay"])
@pytest.mark.parametrize("value", [None, 1, False, [], {}])
def test_text_answers_require_strings(api_client, quiz_case, index, value):
    answers = deepcopy(quiz_case["answers"])
    answers[index]["text"] = value
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")


@pytest.mark.parametrize("index,field", [(0, "choice"), (1, "choice"), (2, "text"), (3, "text")])
def test_type_specific_answer_cannot_be_omitted(api_client, quiz_case, index, field):
    answers = deepcopy(quiz_case["answers"])
    del answers[index][field]
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")


@pytest.mark.parametrize("index,field,value", [(0, "text", ""), (1, "text", "O"),
                                              (2, "choice", 0), (3, "choice", 1)])
def test_answer_fields_cannot_mix_question_types(api_client, quiz_case, index, field, value):
    answers = deepcopy(quiz_case["answers"])
    answers[index][field] = value
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")


@pytest.mark.parametrize("value", [None, "", "certain", "SURE", True, 1, [], {}])
def test_confidence_rejects_null_and_unknown_values(api_client, quiz_case, value):
    answers = deepcopy(quiz_case["answers"])
    answers[0]["confidence"] = value
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")


def test_missing_confidence_uses_dedicated_error_without_writes(api_client, quiz_case, domain_connection):
    answers = deepcopy(quiz_case["answers"])
    del answers[-1]["confidence"]
    before = quiz_effects(domain_connection, quiz_case)
    domain_connection.commit()
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 400, "CONFIDENCE_REQUIRED")
    assert quiz_effects(domain_connection, quiz_case) == before


@pytest.mark.parametrize("location", ["body", "answer"])
def test_unknown_request_fields_are_rejected(api_client, quiz_case, location):
    payload = {"quizId": quiz_case["quiz_id"], "answers": deepcopy(quiz_case["answers"])}
    target = payload if location == "body" else payload["answers"][0]
    target["unexpected"] = True
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"], json=payload)
    assert_error(response, 422, "INVALID_INPUT")


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_saved_grading_requires_boolean(api_client, quiz_case, value):
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": quiz_case["answers"],
                                     "savedGrading": value})
    assert_error(response, 422, "INVALID_INPUT")


def test_openapi_allows_legacy_quiz_id_omission_but_keeps_answer_fields_required(api_client):
    response = api_client.get("/openapi.json")
    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    # FE main49c serializes only answers/savedGrading; explicit IDs remain UUIDs.
    assert set(schemas["SubmitIn"]["required"]) == {"answers"}
    assert schemas["SubmitIn"]["properties"]["quizId"]["format"] == "uuid"
    assert set(schemas["Answer"]["required"]) == {"questionId", "confidence"}
    assert schemas["Answer"]["properties"]["confidence"]["enum"] == ["sure", "unsure"]


def test_all_answers_are_validated_before_any_grading(api_client, quiz_case, domain_connection, monkeypatch):
    answers = deepcopy(quiz_case["answers"])
    answers[3]["text"] = "AI가 호출되면 안 되는 답안"
    del answers[2]["text"]
    # 서술형을 먼저 채점하게 배치해도 뒤 문항의 오류를 먼저 거절해야 한다.
    domain_connection.execute(update(t.quiz_items).where(t.quiz_items.c.id == quiz_case["items"][3]["id"])
                              .values(sort_order=0))
    before = quiz_effects(domain_connection, quiz_case)
    domain_connection.commit()
    grader = Mock(side_effect=AssertionError("invalid submissions must not call a grader"))
    monkeypatch.setattr(QuizService, "_grade_essay", grader)
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert_error(response, 422, "INVALID_INPUT")
    grader.assert_not_called()
    assert quiz_effects(domain_connection, quiz_case) == before


def test_explicit_blank_text_null_unused_fields_and_unsure_are_valid(api_client, quiz_case):
    answers = deepcopy(quiz_case["answers"])
    for index, answer in enumerate(answers):
        answer["confidence"] = "unsure"
        if index < 2:
            answer["text"] = None
            answer["choice"] = 1  # 마지막 보기 역시 유효하다.
        else:
            answer["choice"] = None
            answer["text"] = ""
    response = api_client.post(f"/api/quiz/{quiz_case['quiz_id']}/submit", headers=quiz_case["headers"],
                               json={"quizId": quiz_case["quiz_id"], "answers": answers})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["summary"] == {
        "total": 4, "correct": 0, "sureCorrect": 0, "unsureCorrect": 0, "wrong": 4}
    assert all(item["confidence"] == "unsure" for item in response.json()["data"]["items"])


def test_answer_arrays_have_no_arbitrary_thousand_item_limit(api_client, quiz_case, domain_connection):
    case = quiz_case
    questions = [{**case["questions"][0], "id": str(uuid4()), "code": f"large-set-{index}"}
                 for index in range(1001)]
    domain_connection.execute(t.questions.insert(), questions)
    quiz_id, items = create_quiz(domain_connection, case["user_id"], case["subject_id"],
                                 [question["id"] for question in questions])
    domain_connection.commit()
    answers = [{"questionId": item["id"], "choice": 0, "confidence": "sure"} for item in items]
    response = api_client.post(f"/api/quiz/{quiz_id}/submit", headers=case["headers"],
                               json={"quizId": quiz_id, "answers": answers})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["summary"]["total"] == 1001
    assert response.json()["data"]["summary"]["correct"] == 1001
    assert [item["questionId"] for item in response.json()["data"]["items"]] == [item["id"] for item in items]


@pytest.mark.parametrize("extra", [False, True], ids=["practice", "extra"])
def test_question_and_result_views_preserve_unit_and_material_ids(api_client, quiz_case, domain_connection, extra):
    case = quiz_case
    unit = case["unit"]
    if extra:
        domain_connection.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz_id"])
                                  .values(type="extra"))
        domain_connection.execute(t.quiz_scopes.insert().values(
            id=str(uuid4()), quiz_session_id=case["quiz_id"], unit_id=unit["id"],
            start_page=unit["start_page"], end_page=unit["end_page"]))
        domain_connection.commit()
    questions = api_client.get(f"/api/quiz/{case['quiz_id']}", headers=case["headers"]).json()["data"]["questions"]
    assert all(q["materialId"] == unit["material_id"] and q["unitId"] == unit["id"] for q in questions)
    submitted = api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"],
                                json={"quizId": case["quiz_id"], "answers": case["answers"]})
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()["data"]
    for field in ("items", "unitChanges", "bookChanges"):
        assert result[field]
        assert all(item["materialId"] == unit["material_id"] and item["unitId"] == unit["id"]
                   for item in result[field])
    if extra:
        assert result["unitId"] == unit["id"] and result["materialId"] == unit["material_id"]
        assert result["moreUnitChange"]["unitId"] == unit["id"]
        assert result["moreUnitChange"]["materialId"] == unit["material_id"]


def test_identical_unit_numbers_keep_distinct_material_identity(api_client, quiz_case, domain_connection):
    case = quiz_case
    other = domain_connection.execute(select(t.questions.c.id, t.questions.c.unit_id,
        t.units.c.material_id, t.materials.c.subject_id)
        .join(t.units, t.questions.c.unit_id == t.units.c.id)
        .join(t.materials, t.units.c.material_id == t.materials.c.id)
        .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
        .where(t.subjects.c.user_id == case["user_id"], t.subjects.c.code == "DB")
        .order_by(t.units.c.sort_order, t.questions.c.code)).mappings().first()
    other_quiz, _ = create_quiz(domain_connection, case["user_id"], other["subject_id"], [other["id"]])
    domain_connection.commit()
    first = api_client.get(f"/api/quiz/{case['quiz_id']}", headers=case["headers"]).json()["data"]["questions"][0]
    second = api_client.get(f"/api/quiz/{other_quiz}", headers=case["headers"]).json()["data"]["questions"][0]
    assert first["unitNo"] == second["unitNo"] == 1
    assert first["materialId"] == case["unit"]["material_id"] != second["materialId"] == other["material_id"]
    assert first["unitId"] == case["unit"]["id"] != second["unitId"] == other["unit_id"]
