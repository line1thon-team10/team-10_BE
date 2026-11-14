"""Q-04 provenance extends internal results without inventing legacy facts."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from test_quiz_storage import NOW, make_case, save


@pytest.fixture
def grading_lease(domain_connection):
    conn = domain_connection
    case = make_case(conn)
    answer = save(QuizAnswerRepository(conn), case).answer
    repo = GradingJobRepository(conn)
    job = repo.enqueue(case["user"], case["quiz"], case["essay_item"], revision=1, now=NOW)
    lease = repo.claim(case["user"], job["id"], owner="test", now=NOW, lease_for=timedelta(minutes=1))
    conn.commit()
    provenance = {"answer_hash": answer["answer_hash"], "question_hash": "b" * 64, "source_hash": "c" * 64,
                  "material_hash": "d" * 64, "model_version": "model-v1", "prompt_version": "prompt-v1",
                  "schema_version": "essay-v1", "saved_result_id": None}
    return case, lease, provenance


def finish(conn, case, lease, result):
    return GradingJobRepository(conn).complete(case["user"], lease["id"], owner="test", token=lease["lease_token"],
        revision=1, now=NOW + timedelta(seconds=1), result={"is_correct": True, "essay_score": 85, **result})


def test_new_result_keeps_exact_version_evidence_without_finalizing_attempts(domain_connection, grading_lease):
    case, lease, provenance = grading_lease
    assert finish(domain_connection, case, lease, {"provenance": provenance})
    result = domain_connection.scalar(select(t.quiz_grading_jobs.c.result))
    assert result["provenance"] == provenance
    assert domain_connection.execute(select(t.quiz_attempts)).first() is None
    assert domain_connection.execute(select(t.mastery_history)).first() is None


def test_legacy_result_has_explicitly_unknown_provenance(domain_connection, grading_lease):
    case, lease, _ = grading_lease
    assert finish(domain_connection, case, lease, {})
    assert domain_connection.scalar(select(t.quiz_grading_jobs.c.result))["provenance"] is None


@pytest.mark.parametrize("field,value", [("question_hash", "x" * 64), ("source_hash", "A" * 64),
    ("material_hash", "a" * 63), ("answer_hash", "a" * 64), ("model_version", " "),
    ("prompt_version", "x\x00y"), ("schema_version", "x\ud800"), ("schema_version", float("nan")),
    ("model_version", True), ("saved_result_id", "bad"),
    pytest.param("saved_result_id", str(uuid4()), id="foreign-saved-result-id"), ("extra", 1)])
def test_invalid_provenance_cannot_mark_job_succeeded(domain_connection, grading_lease, field, value):
    case, lease, provenance = grading_lease
    with pytest.raises(ApiError) as error:
        finish(domain_connection, case, lease, {"provenance": {**provenance, field: value}})
    assert (error.value.code, error.value.status) == ("INVALID_INPUT", 422)
    assert domain_connection.scalar(select(t.quiz_grading_jobs.c.status)) == "running"
    assert domain_connection.scalar(select(t.quiz_grading_jobs.c.result)) is None


@pytest.mark.parametrize("result", [{"feedback": "x\x00y"}, {"got_points": ["\udfff"]},
                                  {"missing_points": [float("inf")]}, {"provenance": []}])
def test_db_unsupported_result_text_and_numbers_are_rejected_before_sql(domain_connection, grading_lease, result):
    case, lease, _ = grading_lease
    with pytest.raises(ApiError) as error:
        finish(domain_connection, case, lease, result)
    assert error.value.status == 422
    assert domain_connection.scalar(select(t.quiz_grading_jobs.c.status)) == "running"


@pytest.mark.parametrize("owner,kind,schema,valid", [("self", "essay_grade", "essay-v1", True),
    ("other", "essay_grade", "essay-v1", False), (None, "essay_grade", "essay-v1", False),
    ("self", "tutor", "essay-v1", False), ("self", "essay_grade", "essay-v2", False)])
def test_saved_result_reference_must_belong_to_exact_owner_kind_and_schema(domain_connection, grading_lease, owner, kind, schema, valid):
    conn = domain_connection
    case, lease, provenance = grading_lease
    user_id = case["user"] if owner == "self" else make_case(conn)["user"] if owner == "other" else None
    saved_id = str(uuid4())
    conn.execute(t.ai_saved_results.insert().values(id=saved_id, user_id=user_id, kind=kind, input_hash="a" * 64,
        schema_version=schema, result={"score": 85}, validated_at=NOW, created_at=NOW))
    if valid:
        assert finish(conn, case, lease, {"provenance": {**provenance, "saved_result_id": saved_id}})
    else:
        with pytest.raises(ApiError) as error:
            finish(conn, case, lease, {"provenance": {**provenance, "saved_result_id": saved_id}})
        assert error.value.status == 422
