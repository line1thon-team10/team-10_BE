from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

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
    questions = response.json()["data"]["questions"]
    item_ids = [item["id"] for item in case["items"]]
    assert [q["id"] for q in questions] == item_ids
    assert set(item_ids).isdisjoint(q["id"] for q in case["questions"])
    submitted = api_client.post(f"/api/quiz/{case['quiz_id']}/submit", headers=case["headers"],
                                json={"answers": case["answers"]})
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
