from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository, canonical_hash
from app.repositories.grading_jobs import GradingJobRepository

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


@pytest.fixture
def queued_job(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    save(QuizAnswerRepository(conn), case)
    job = GradingJobRepository(conn).enqueue(case["user"], case["quiz"], case["essay_item"], revision=1, now=NOW)
    conn.commit()
    return job


def claim(repo, case, job, **changes):
    return repo.claim(case["user"], job["id"], **{"owner": "worker-a", "now": NOW,
                      "lease_for": timedelta(minutes=1), **changes})


def complete(repo, case, lease, **changes):
    return repo.complete(case["user"], lease["id"], **{
        "owner": lease["lease_owner"], "token": lease["lease_token"], "revision": lease["answer_revision"],
        "now": NOW + timedelta(seconds=10), "result": {"is_correct": True, "essay_score": 85}, **changes})


def test_enqueue_is_unique_per_revision_and_rollback_is_atomic(domain_connection, storage_case, queued_job):
    conn, case = domain_connection, storage_case
    jobs = GradingJobRepository(conn)
    assert jobs.enqueue(case["user"], case["quiz"], case["essay_item"], revision=1, now=NOW) == queued_job
    assert len(conn.execute(select(t.quiz_grading_jobs)).all()) == 1
    save(QuizAnswerRepository(conn), case, revision=1, text="수정")
    jobs.enqueue(case["user"], case["quiz"], case["essay_item"], revision=2, now=NOW)
    conn.rollback()
    assert jobs.get(case["user"], queued_job["id"])["status"] == "queued"
    assert QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"])["revision"] == 1


def test_live_lease_blocks_duplicate_worker_and_one_completion_wins(domain_connection, storage_case, queued_job):
    conn, case = domain_connection, storage_case
    jobs = GradingJobRepository(conn)
    lease = claim(jobs, case, queued_job)
    assert lease["status"] == "running" and lease["claim_count"] == 1
    assert claim(jobs, case, queued_job, owner="worker-b") is None
    assert jobs.available(case["user"], now=NOW) == []
    assert complete(jobs, case, lease)
    assert not complete(jobs, case, lease, result={"is_correct": False, "essay_score": 0})
    finished = jobs.get(case["user"], lease["id"])
    assert finished["result"]["essay_score"] == 85 and finished["status"] == "succeeded"
    assert finished["lease_owner"] is finished["lease_token"] is finished["lease_expires_at"] is None
    assert conn.execute(select(t.quiz_attempts)).first() is None
    assert conn.execute(select(t.mastery_history)).first() is None


def test_new_connection_reclaims_expired_lease_and_rejects_late_worker(api_client, domain_connection, storage_case, queued_job):
    from app.db import session
    conn, case = domain_connection, storage_case
    jobs = GradingJobRepository(conn)
    first = claim(jobs, case, queued_job)
    conn.commit()
    expired = NOW + timedelta(minutes=1)
    with session.engine.begin() as restarted:
        recovered = GradingJobRepository(restarted)
        assert [job["id"] for job in recovered.available(case["user"], now=expired)] == [queued_job["id"]]
        second = claim(recovered, case, queued_job, owner="worker-b", now=expired)
    assert second["lease_token"] != first["lease_token"] and second["claim_count"] == 2
    assert not complete(jobs, case, first, now=expired + timedelta(seconds=1))
    assert complete(jobs, case, second, now=expired + timedelta(seconds=2))


@pytest.mark.parametrize("change", [{"owner": "worker-b"}, {"token": str(uuid4())}, {"revision": 2},
                                    {"revision": True}, {"now": NOW + timedelta(minutes=1)},
                                    {"now": NOW - timedelta(seconds=1)}])
def test_lease_owner_token_revision_and_time_are_all_required(domain_connection, storage_case, queued_job, change):
    jobs = GradingJobRepository(domain_connection)
    lease = claim(jobs, storage_case, queued_job)
    assert not complete(jobs, storage_case, lease, **change)
    assert jobs.get(storage_case["user"], lease["id"])["status"] == "running"


def test_answer_edit_invalidates_running_worker_and_previous_revision_enqueue(domain_connection, storage_case, queued_job):
    conn, case = domain_connection, storage_case
    jobs = GradingJobRepository(conn)
    lease = claim(jobs, case, queued_job)
    save(QuizAnswerRepository(conn), case, revision=1, text="바뀐 원문")
    assert not complete(jobs, case, lease)
    assert claim(jobs, case, queued_job, now=NOW + timedelta(minutes=2)) is None
    error("ANSWER_CONFLICT", lambda: jobs.enqueue(case["user"], case["quiz"], case["essay_item"], revision=1, now=NOW))
    next_job = jobs.enqueue(case["user"], case["quiz"], case["essay_item"], revision=2, now=NOW)
    assert next_job["id"] != queued_job["id"]
    assert [job["id"] for job in jobs.available(case["user"], now=NOW)] == [next_job["id"]]


def test_failure_keeps_answer_and_retry_uses_new_token(domain_connection, storage_case, queued_job):
    conn, case = domain_connection, storage_case
    jobs = GradingJobRepository(conn)
    lease = claim(jobs, case, queued_job)
    assert jobs.fail(case["user"], lease["id"], owner="worker-a", token=lease["lease_token"], revision=1,
                     now=NOW + timedelta(seconds=1), error_code="AI_FAILED")
    failed = jobs.get(case["user"], lease["id"])
    assert failed["status"] == "failed" and failed["result"] is None
    assert QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"])["text"] == "  원문  답안\n"
    retried = claim(jobs, case, queued_job, now=NOW + timedelta(seconds=2))
    assert retried["claim_count"] == 2 and retried["lease_token"] != lease["lease_token"]
    assert not complete(jobs, case, lease)
    assert complete(jobs, case, retried)


def test_renew_requires_current_live_lease_and_never_shortens_it(domain_connection, storage_case, queued_job):
    jobs = GradingJobRepository(domain_connection)
    lease = claim(jobs, storage_case, queued_job)
    args = {"owner": "worker-a", "token": lease["lease_token"], "revision": 1,
            "now": NOW + timedelta(seconds=10), "lease_for": timedelta(seconds=1)}
    assert jobs.renew(storage_case["user"], lease["id"], **args)
    assert jobs.get(storage_case["user"], lease["id"])["lease_expires_at"] == NOW + timedelta(minutes=1)
    args["lease_for"] = timedelta(minutes=2)
    assert jobs.renew(storage_case["user"], lease["id"], **args)
    assert jobs.get(storage_case["user"], lease["id"])["lease_expires_at"] == NOW + timedelta(minutes=2, seconds=10)
    assert not jobs.renew(storage_case["user"], lease["id"], **{**args, "owner": "other"})
    assert not jobs.renew(storage_case["user"], lease["id"], **{**args, "now": NOW + timedelta(minutes=3)})


@pytest.mark.parametrize("result", [{"is_correct": True, "essay_score": score} for score in (-1, 101, True, 85.0, "85", None, float("nan"))]
    + [{"is_correct": True, "essay_score": 84}, {"is_correct": 1, "essay_score": 85},
       {"is_correct": True, "essay_score": 85, "got_points": "bad"},
       {"is_correct": True, "essay_score": 85, "missing_points": [1]},
       {"is_correct": True, "essay_score": 85, "grading_status": "failed"},
       {"is_correct": True, "essay_score": 85, "extra": 1}])
def test_invalid_grading_result_never_finishes_job(domain_connection, storage_case, queued_job, result):
    jobs = GradingJobRepository(domain_connection)
    lease = claim(jobs, storage_case, queued_job)
    error("INVALID_INPUT", lambda: complete(jobs, storage_case, lease, result=result), 422)
    current = jobs.get(storage_case["user"], lease["id"])
    assert current["status"] == "running" and current["result"] is None


@pytest.mark.parametrize("score", [0, 84, 85, 100])
def test_grading_score_boundaries_are_stored(domain_connection, storage_case, queued_job, score):
    jobs = GradingJobRepository(domain_connection)
    lease = claim(jobs, storage_case, queued_job)
    assert complete(jobs, storage_case, lease, result={"is_correct": score >= 85, "essay_score": score})


def test_foreign_user_cannot_read_claim_or_finish_a_job(domain_connection, storage_case, queued_job):
    conn, case = domain_connection, storage_case
    other = make_case(conn)
    jobs = GradingJobRepository(conn)
    assert jobs.available(other["user"], now=NOW) == []
    error("NO_QUIZ", lambda: jobs.get(other["user"], queued_job["id"]), 404)
    error("NO_QUIZ", lambda: claim(jobs, other, queued_job), 404)
    lease = claim(jobs, case, queued_job)
    error("NO_QUIZ", lambda: complete(jobs, other, lease), 404)


@pytest.mark.parametrize("change", [{"lease_for": timedelta(0)}, {"lease_for": timedelta(seconds=-1)},
                                    {"lease_for": 60}, {"owner": " "}, {"owner": None}])
def test_invalid_lease_settings_do_not_claim(domain_connection, storage_case, queued_job, change):
    jobs = GradingJobRepository(domain_connection)
    error("INVALID_INPUT", lambda: claim(jobs, storage_case, queued_job, **change), 422)
    assert jobs.get(storage_case["user"], queued_job["id"])["status"] == "queued"


def seal_answers(conn, case):
    repo = QuizAnswerRepository(conn)
    for kind in ("choice", "essay"):
        save(repo, case, kind=kind)
    return repo.seal(case["user"], case["quiz"], now=NOW + timedelta(seconds=1),
                     expected_revisions={case["choice_item"]: 1, case["essay_item"]: 1})


def test_seal_freezes_exact_revisions_and_replays_original_state(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    repo = QuizAnswerRepository(conn)
    state = seal_answers(conn, case)
    assert state["input_recovered"] and not state["legacy"] and len(state["answer_set_hash"]) == 64
    assert state["result_snapshot"] is None
    replay = repo.seal(case["user"], case["quiz"], now=NOW + timedelta(days=1),
                       expected_revisions={case["essay_item"]: 1, case["choice_item"]: 1})
    assert replay == state
    assert not save(repo, case, revision=1).created
    error("QUIZ_FINALIZED", lambda: save(repo, case, revision=1, text="봉인 뒤 변경"))
    error("QUIZ_FINALIZED", lambda: repo.seal(case["user"], case["quiz"], now=NOW,
          expected_revisions={case["essay_item"]: 2, case["choice_item"]: 1}))
    conn.commit()
    assert QuizAnswerRepository(conn).submission(case["user"], case["quiz"]) == state


@pytest.mark.parametrize("kind", ["missing_answer", "missing_item", "foreign_item", "stale_revision", "duplicate_uuid"])
def test_incomplete_or_stale_sets_cannot_be_sealed(domain_connection, storage_case, kind):
    conn, case = domain_connection, storage_case
    repo = QuizAnswerRepository(conn)
    save(repo, case)
    expected = {case["essay_item"]: 1, case["choice_item"]: 1}
    if kind != "missing_answer":
        save(repo, case, kind="choice")
    if kind == "missing_item":
        del expected[case["choice_item"]]
    elif kind == "foreign_item":
        expected[str(uuid4())] = expected.pop(case["choice_item"])
    elif kind == "stale_revision":
        save(repo, case, revision=1, text="바뀜")
    elif kind == "duplicate_uuid":
        expected[case["essay_item"].replace("-", "").upper()] = 1
    error("ANSWER_CONFLICT" if kind == "stale_revision" else "INVALID_INPUT",
          lambda: repo.seal(case["user"], case["quiz"], expected_revisions=expected, now=NOW),
          409 if kind == "stale_revision" else 422)
    assert repo.submission(case["user"], case["quiz"]) is None


def test_foreign_user_cannot_seal_a_quiz(domain_connection, storage_case):
    conn, case = domain_connection, storage_case
    other = make_case(conn)
    repo = QuizAnswerRepository(conn)
    seal_answers(conn, case)
    error("NO_QUIZ", lambda: repo.seal(other["user"], case["quiz"], now=NOW, expected_revisions={}), 404)
    error("NO_QUIZ", lambda: repo.submission(other["user"], case["quiz"]), 404)
