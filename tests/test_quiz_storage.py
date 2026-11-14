from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository, canonical_hash

NOW = datetime(2026, 11, 14, 3, tzinfo=timezone.utc)
DAY = date(2026, 11, 14)


def make_case(conn):
    ids = {name: str(uuid4()) for name in ("user", "subject", "material", "unit", "quiz", "choice", "essay", "choice_item", "essay_item")}
    conn.execute(t.users.insert().values(id=ids["user"], created_at=NOW, updated_at=NOW))
    conn.execute(t.subjects.insert().values(id=ids["subject"], user_id=ids["user"], code="DB", name="자료", color="#123456", created_at=NOW, updated_at=NOW))
    conn.execute(t.materials.insert().values(id=ids["material"], subject_id=ids["subject"], original_name="book.pdf", source_format="pdf", sha256="a" * 64, analysis_version="1", created_at=NOW, updated_at=NOW))
    conn.execute(t.units.insert().values(id=ids["unit"], material_id=ids["material"], code="DB1", title="단원", sort_order=1, created_at=NOW, updated_at=NOW))
    conn.execute(t.quiz_sessions.insert().values(id=ids["quiz"], user_id=ids["user"], subject_id=ids["subject"], type="practice", quiz_date=DAY, status="ready", created_at=NOW))
    for index, kind in enumerate(("choice", "essay"), 1):
        conn.execute(t.questions.insert().values(id=ids[kind], unit_id=ids["unit"], code=kind,
            type=kind, difficulty="low", prompt=kind, choices=[{"text": "맞음"}, {"text": "틀림"}] if kind == "choice" else None,
            correct_answer="맞음", created_at=NOW))
        conn.execute(t.quiz_items.insert().values(id=ids[f"{kind}_item"], quiz_session_id=ids["quiz"],
            question_id=ids[kind], sort_order=index, inclusion_reason="practice"))
    conn.commit()
    return ids


@pytest.fixture
def storage_case(domain_connection):
    return make_case(domain_connection)


def save(repo, case, *, kind="essay", revision=0, **changes):
    payload = {"choice": 0} if kind == "choice" else {"text": "  원문  답안\n"}
    return repo.save(case["user"], case["quiz"], case[f"{kind}_item"], revision=revision,
                     **{**payload, "confidence": "sure", "now": NOW, "answered_date": DAY, **changes})


def error(code, action, status=409):
    with pytest.raises(ApiError) as caught:
        action()
    assert caught.value.code == code and caught.value.status == status


def test_save_preserves_original_text_and_survives_reopen(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    first = save(QuizAnswerRepository(conn), case)
    assert first.created and first.is_current and first.answer["revision"] == 1
    assert first.answer["text"] == "  원문  답안\n"
    assert first.answer["answer_hash"] == canonical_hash({"confidence": "sure", "text": "  원문  답안\n", "choice": None})
    conn.commit()
    reopened = QuizAnswerRepository(conn)
    assert reopened.current(case["user"], case["quiz"], case["essay_item"]) == first.answer
    assert reopened.list_current(case["user"], case["quiz"]) == [first.answer]
    assert conn.execute(select(t.quiz_attempts)).first() is None


def test_same_answer_retries_never_increase_revision_or_rewind_head(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    repo = QuizAnswerRepository(conn)
    first = save(repo, case)
    for revision in (0, 1, 1, 0):
        replay = save(repo, case, revision=revision, now=NOW + timedelta(hours=1))
        assert replay.answer == first.answer and not replay.created and replay.is_current
    second = save(repo, case, revision=1, text="다른 답")
    assert second.created and second.answer["revision"] == 2
    replay = save(repo, case, revision=0)
    assert replay.answer == first.answer and not replay.created and not replay.is_current
    assert repo.current(case["user"], case["quiz"], case["essay_item"]) == second.answer
    error("ANSWER_CONFLICT", lambda: save(repo, case, revision=0, text="세 번째 답"))
    error("ANSWER_CONFLICT", lambda: save(repo, case, revision=3, text="미래 답"))
    assert len(conn.execute(select(t.quiz_answers)).all()) == 2


@pytest.mark.parametrize("changed", [{"text": "원문 답안"}, {"confidence": "unsure"}])
def test_text_whitespace_and_confidence_changes_create_new_revision(domain_connection, storage_case, changed):
    repo = QuizAnswerRepository(domain_connection)
    first = save(repo, storage_case)
    second = save(repo, storage_case, revision=1, **changed)
    assert second.answer["revision"] == 2 and second.answer["answer_hash"] != first.answer["answer_hash"]


@pytest.mark.parametrize("change", [{"revision": -1}, {"revision": True}, {"revision": "0"}, {"text": None},
    {"text": 1}, {"choice": 0}, {"confidence": None}, {"confidence": "other"}, {"now": datetime(2026, 11, 14)},
    {"answered_date": NOW}])
def test_invalid_answer_never_creates_a_revision(domain_connection, storage_case, change):
    repo = QuizAnswerRepository(domain_connection)
    error("INVALID_INPUT", lambda: save(repo, storage_case, **change), 422)
    assert domain_connection.execute(select(t.quiz_answers)).first() is None


@pytest.mark.parametrize("choice", [True, "0", 0.0, -1, 2, None])
def test_choice_checks_use_strict_type_and_actual_options(domain_connection, storage_case, choice):
    repo = QuizAnswerRepository(domain_connection)
    error("INVALID_INPUT", lambda: save(repo, storage_case, kind="choice", choice=choice), 422)


def test_foreign_quiz_item_and_question_ownership_are_checked(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    other = make_case(conn)
    repo = QuizAnswerRepository(conn)
    error("NO_QUIZ", lambda: repo.current(other["user"], case["quiz"], case["essay_item"]), 404)
    error("NO_QUIZ", lambda: save(repo, {**case, "user": other["user"]}), 404)
    error("INVALID_INPUT", lambda: save(repo, {**case, "essay_item": other["essay_item"]}), 422)
    conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == case["essay_item"]).values(question_id=other["essay"]))
    error("NO_QUIZ", lambda: save(repo, case), 404)
    assert conn.execute(select(t.quiz_answers)).first() is None


def test_outer_rollback_removes_answer_and_head(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    save(QuizAnswerRepository(conn), case)
    conn.rollback()
    assert conn.execute(select(t.quiz_answers)).first() is None
    assert conn.execute(select(t.quiz_answer_heads)).first() is None


def test_changed_answer_cancels_old_running_job_without_touching_old_answer(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    repo = QuizAnswerRepository(conn)
    first = save(repo, case)
    job_id = str(uuid4())
    conn.execute(t.quiz_grading_jobs.insert().values(id=job_id, answer_id=first.answer["id"], answer_revision=1,
        status="running", lease_owner="worker-a", lease_token=str(uuid4()), lease_expires_at=NOW + timedelta(minutes=1),
        claimed_at=NOW, created_at=NOW))
    second = save(repo, case, revision=1, text="수정 답")
    job = conn.execute(select(t.quiz_grading_jobs)).mappings().one()
    assert job["status"] == "cancelled" and job["error_code"] == "ANSWER_SUPERSEDED"
    assert job["lease_owner"] is job["lease_token"] is job["lease_expires_at"] is job["result"] is None
    assert second.answer["revision"] == 2
    assert conn.scalar(select(t.quiz_answers.c.text).where(t.quiz_answers.c.id == first.answer["id"])) == first.answer["text"]


def test_closed_legacy_quiz_rejects_new_answers(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(status="submitted", finalized_at=NOW))
    error("QUIZ_FINALIZED", lambda: save(QuizAnswerRepository(conn), case))


def test_real_lock_conflict_does_not_create_an_answer(api_client, domain_connection, storage_case):
    from app.db import session
    conn, case = domain_connection, storage_case
    repo = QuizAnswerRepository(conn)
    repo.lock_quiz(case["user"], case["quiz"])
    try:
        with session.engine.connect() as competitor:
            if competitor.dialect.name == "sqlite":
                competitor.exec_driver_sql("PRAGMA busy_timeout=20")
                competitor.commit()
            error("REQUEST_IN_PROGRESS", lambda: save(QuizAnswerRepository(competitor), case))
    finally:
        conn.rollback()
    assert conn.execute(select(t.quiz_answers)).first() is None
