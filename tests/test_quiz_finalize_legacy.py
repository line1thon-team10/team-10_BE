from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.clock import now_utc
from app.db import schema as t
from tests.test_quiz_answer_api import answer_case
from tests.test_quiz_finalize import get_result, submit


def completed_legacy(conn, case, *, incomplete=False):
    at = now_utc() - timedelta(days=3)
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(
        status="submitted", grading_status="completed", submitted_at=at, finalized_at=at, graded_at=at))
    # The original choice index cannot be recovered from this preserved text.
    conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(
        choices=[{"text": "같은 보기"}, {"text": "같은 보기"}]))
    for kind in ("choice", "ox", "short", "essay"):
        if incomplete and kind == "essay":
            continue
        conn.execute(t.quiz_attempts.insert().values(id=str(uuid4()), quiz_item_id=case[f"{kind}_item"],
            answer="같은 보기" if kind == "choice" else "보존 원문", confidence="sure", is_correct=kind != "essay",
            grading_status="graded", essay_score=70 if kind == "essay" else None,
            answered_at=at, answered_date=at.date(), finalized_at=at, graded_at=at, mastery_applied_at=at))
    conn.commit()
    return at


@pytest.mark.parametrize("sidecar", [False, True])
@pytest.mark.parametrize("archived", [False, True])
def test_legacy_reconstruction_is_frozen_without_rewriting_original_facts(
        api_client, answer_case, domain_connection, sidecar, archived):
    case, conn = answer_case, domain_connection
    at = completed_legacy(conn, case)
    if sidecar:
        conn.execute(t.quiz_submission_states.insert().values(quiz_session_id=case["quiz"], sealed_at=at,
            input_recovered=False, sealed_answers=[], legacy=True, legacy_snapshot={"preserved": "migration facts"}))
    if archived:
        conn.execute(update(t.subjects).where(t.subjects.c.id == case["subject"]).values(status="archived"))
        conn.execute(update(t.units).where(t.units.c.id == case["unit"]).values(retired_at=at))
    tables = (t.quiz_attempts, t.quiz_sessions, t.units, t.mastery_history, t.weak_questions)
    before = {table.name: conn.execute(select(table)).all() for table in tables}
    conn.commit()
    response = get_result(api_client, case)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["data"]["summary"]["total"] == 4
    assert {table.name: conn.execute(select(table)).all() for table in tables} == before
    state = conn.execute(select(t.quiz_submission_states)).mappings().one()
    assert state["legacy"] and state["result_provenance"] == "legacy_reconstructed"
    assert state["result_schema_version"] == "quiz-result-legacy-v1" and state["result_captured_at"] is not None
    assert not state["input_recovered"] and state["answer_set_hash"] is None
    if sidecar:
        assert state["legacy_snapshot"] == {"preserved": "migration facts"}
    else:
        assert state["legacy_snapshot"]["schemaVersion"] == "legacy-current-facts-v1"
    assert conn.execute(select(t.quiz_answers)).first() is None
    assert conn.execute(select(t.quiz_grading_jobs)).first() is None
    conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(prompt="나중 수정"))
    conn.commit()
    assert get_result(api_client, case).json() == result
    assert submit(api_client, case).status_code == 409
    assert conn.execute(select(t.quiz_submission_states)).mappings().one() == state


def test_incomplete_legacy_facts_are_not_published_as_a_complete_result(api_client, answer_case, domain_connection):
    completed_legacy(domain_connection, answer_case, incomplete=True)
    response = get_result(api_client, answer_case)
    assert response.status_code == 503 and response.json()["error"]["code"] == "AI_FAILED"
    assert domain_connection.execute(select(t.quiz_submission_states)).first() is None
