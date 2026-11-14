from datetime import timedelta

import pytest
from sqlalchemy import JSON, select, update

from app.ai.gateway import FunctionRef, ProviderSpec
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import ExecutionModeConflict, GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services.grading import GradingState, PreparedGrading, validated_completed_result
from test_grading_worker import gateway_factory, job_row, no_gateway, queue, service, worker_case, worker_engine
from test_quiz_storage import NOW, save


FORBIDDEN = ProviderSpec(FunctionRef("never_initialize_provider_or_credentials", "provider"))


@pytest.mark.parametrize("mode", ["saved", "backup"])
def test_enqueue_defaults_never_upgrade_existing_mode_and_conflicts_are_typed(domain_connection, worker_engine, worker_case, mode):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, mode=mode)
    repo = GradingJobRepository(conn)
    assert repo.enqueue(case["user"], case["quiz"], case["essay_item"], revision=1, now=NOW)["execution_mode"] == mode
    with pytest.raises(ExecutionModeConflict) as error:
        repo.enqueue(case["user"], case["quiz"], case["essay_item"], revision=1, now=NOW, mode="live")
    assert (error.value.stored_mode, error.value.requested_mode) == (mode, "live")
    conn.commit()
    outcome = service(worker_engine, gateway_factory=no_gateway).run_job(case["user"], job["id"], mode="live")
    assert outcome.state is GradingState.MODE_CONFLICT and outcome.error_code is None
    assert (outcome.stored_mode, outcome.requested_mode) == (mode, "live")
    assert job_row(conn, job)["execution_mode"] == mode and job_row(conn, job)["status"] == "queued"


@pytest.mark.parametrize("mode", ["saved", "backup"])
def test_restart_after_prepared_claim_keeps_saved_policy_on_expired_lease(domain_connection, worker_engine, worker_case, mode):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, mode=mode)
    worker = service(worker_engine, provider=FORBIDDEN)
    with worker_engine.begin() as transaction:
        prepared = worker.prepare(transaction, case["user"], job["id"])
    assert isinstance(prepared, PreparedGrading) and prepared.snapshot.mode == mode
    # Simulate process loss: the in-memory object is discarded, but DB policy remains.
    restarted = service(worker_engine, owner="restarted", clock=lambda: NOW + timedelta(minutes=1), provider=FORBIDDEN)
    result = restarted.run_next()
    assert result.state == "failed" and result.error_code == "AI_FAILED"
    row = job_row(conn, job)
    assert row["execution_mode"] == mode and row["claim_count"] == 2 and row["result"] is None
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    assert conn.scalar(select(t.quiz_grading_jobs.c.id).where(t.quiz_grading_jobs.c.result.is_(None))) == job["id"]
    conn.rollback()
    assert restarted.run_next().state == "idle"
    assert worker.execute(prepared).state == "stale"  # It cannot start a late live request either.


@pytest.mark.parametrize("mode", ["saved", "backup"])
def test_live_running_lease_rejects_saved_retry_without_changing_policy_or_starting_transport(domain_connection, worker_engine, worker_case, mode):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    lease = GradingJobRepository(conn).claim(case["user"], job["id"], owner="active-live", now=NOW,
                                            lease_for=timedelta(minutes=1))
    conn.commit()
    result = service(worker_engine, gateway_factory=no_gateway).run_job(case["user"], job["id"], retry_failed=True, mode=mode)
    assert result.state == "not_claimed"
    row = job_row(conn, job)
    assert row["execution_mode"] == "live" and row["lease_token"] == lease["lease_token"] and row["claim_count"] == 1


@pytest.mark.parametrize("status", ["queued", "failed", "expired"])
def test_explicit_retry_switches_mode_with_new_lease_token_atomically(domain_connection, worker_engine, worker_case, status):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    repo, previous = GradingJobRepository(conn), None
    if status != "queued":
        previous = repo.claim(case["user"], job["id"], owner="previous", now=NOW, lease_for=timedelta(seconds=1))
        if status == "failed":
            assert repo.fail(case["user"], job["id"], owner="previous", token=previous["lease_token"], revision=1,
                now=NOW + timedelta(milliseconds=500), error_code="AI_FAILED")
    conn.commit()
    worker = service(worker_engine, clock=lambda: NOW + timedelta(seconds=2), provider=FORBIDDEN)
    with worker_engine.begin() as transaction:
        prepared = worker.prepare(transaction, case["user"], job["id"], retry_failed=True, mode="saved")
        assert isinstance(prepared, PreparedGrading) and prepared.snapshot.mode == "saved"
    row = job_row(conn, job)
    assert row["execution_mode"] == "saved" and row["lease_token"] == prepared.snapshot.token
    if previous:
        assert prepared.snapshot.token != previous["lease_token"]
        assert not repo.complete(case["user"], job["id"], owner="previous", token=previous["lease_token"], revision=1,
            now=NOW + timedelta(seconds=2), result={"is_correct": True, "essay_score": 100})
    conn.rollback()
    assert worker.execute(prepared).state == "failed"  # No matching saved result, never a provider request.
    assert conn.execute(select(t.ai_usage_logs)).first() is None


def test_saved_to_live_requires_an_explicit_retry(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, mode="saved")
    worker = service(worker_engine, gateway_factory=gateway_factory())
    assert worker.run_job(case["user"], job["id"], mode="live").state == "mode_conflict"
    assert worker.run_job(case["user"], job["id"], mode="live", retry_failed=True).state == "succeeded"
    assert job_row(conn, job)["execution_mode"] == "live"


def test_competing_mode_retry_obeys_real_user_lock(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    GradingJobRepository(conn).claim(case["user"], job["id"], owner="first", now=NOW, lease_for=timedelta(seconds=1))
    try:
        with pytest.raises(ApiError) as error:
            service(worker_engine, clock=lambda: NOW + timedelta(seconds=2), gateway_factory=no_gateway).run_job(
                case["user"], job["id"], retry_failed=True, mode="saved")
        assert error.value.code == "REQUEST_IN_PROGRESS"
    finally:
        conn.rollback()
    assert job_row(conn, job)["status"] == "queued" and job_row(conn, job)["execution_mode"] == "live"


def test_rollback_of_answer_enqueue_and_prepare_does_not_leave_a_claim(domain_connection, worker_engine, worker_case):
    case = worker_case
    worker = service(worker_engine, provider=FORBIDDEN)
    with pytest.raises(RuntimeError, match="abort"), worker_engine.begin() as transaction:
        answer = save(QuizAnswerRepository(transaction), case).answer
        job = GradingJobRepository(transaction).enqueue(case["user"], case["quiz"], case["essay_item"],
            revision=answer["revision"], now=NOW, mode="saved")
        assert isinstance(worker.prepare(transaction, case["user"], job["id"]), PreparedGrading)
        raise RuntimeError("abort before commit")
    assert domain_connection.execute(select(t.quiz_answers)).first() is None
    assert domain_connection.execute(select(t.quiz_grading_jobs)).first() is None


def test_delayed_prepare_rechecks_source_before_any_external_work(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    worker = service(worker_engine, gateway_factory=no_gateway)
    with worker_engine.begin() as transaction:
        prepared = worker.prepare(transaction, case["user"], job["id"])
    conn.execute(update(t.question_sources).where(t.question_sources.c.id == case["source"]).values(quote="변경된 근거"))
    conn.commit()
    result = worker.execute(prepared)
    assert result.state == "failed" and result.error_code == "GRADING_INPUT_CHANGED"
    assert job_row(conn, job)["result"] is None


def test_duplicate_execute_cannot_repeat_transport_or_overwrite_success(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    calls = []
    worker = service(worker_engine, gateway_factory=gateway_factory(hook=lambda _: calls.append(True)))
    with worker_engine.begin() as transaction:
        prepared = worker.prepare(transaction, case["user"], job["id"])
    assert worker.execute(prepared).state == "succeeded"
    assert worker.execute(prepared).state == "stale" and calls == [True]


def test_json_null_success_is_not_a_reusable_completed_result(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    conn.execute(update(t.quiz_grading_jobs).where(t.quiz_grading_jobs.c.id == job["id"]).values(status="succeeded", result=JSON.NULL))
    conn.commit()
    with worker_engine.begin() as transaction:
        assert validated_completed_result(transaction, case["user"], job["id"]) is None
    assert service(worker_engine, gateway_factory=no_gateway).run_next().state == "idle"


@pytest.mark.parametrize("mode", [True, 1, "", "LIVE", "live\x00", "saved\ud800"])
def test_invalid_mode_never_creates_or_claims_job(domain_connection, worker_case, mode):
    conn, case = domain_connection, worker_case
    save(QuizAnswerRepository(conn), case)
    with pytest.raises(ApiError) as error:
        GradingJobRepository(conn).enqueue(case["user"], case["quiz"], case["essay_item"], revision=1, now=NOW, mode=mode)
    assert error.value.status == 422 and conn.execute(select(t.quiz_grading_jobs)).first() is None
