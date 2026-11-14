from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services.grading import validated_completed_result
from test_grading_worker import no_gateway, queue, service, worker_case, worker_engine
from test_quiz_storage import NOW


def sealed_jobs(conn, case):
    jobs = {kind: queue(conn, case, kind=kind, **({"text": ""} if kind != "choice" else {}))
            for kind in ("choice", "essay", "short")}
    QuizAnswerRepository(conn).seal(case["user"], case["quiz"], now=NOW,
        expected_revisions={case[f"{kind}_item"]: 1 for kind in jobs})
    conn.commit()
    return jobs


def test_explicit_retry_requeues_only_stale_success_and_pins_saved_mode(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = sealed_jobs(conn, case)["choice"]
    assert service(worker_engine, gateway_factory=no_gateway).run_job(case["user"], job["id"]).state == "succeeded"
    repo = GradingJobRepository(conn)
    assert validated_completed_result(conn, case["user"], job["id"]) is not None
    assert not repo.submission_retry(case["user"], job["id"], revision=1, now=NOW, mode="saved")
    conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(explanation="바뀐 해설"))
    assert validated_completed_result(conn, case["user"], job["id"]) is None
    assert repo.submission_retry(case["user"], job["id"], revision=1, now=NOW, mode="saved", stale_succeeded=True)
    fresh = repo.get(case["user"], job["id"])
    assert (fresh["status"], fresh["execution_mode"], fresh["result"], fresh["lease_token"]) == ("queued", "saved", None, None)
    assert fresh["claim_count"] == 1 and fresh["answer_revision"] == 1
    conn.commit()
    assert service(worker_engine, gateway_factory=no_gateway).run_job(case["user"], job["id"]).state == "succeeded"


def test_live_lease_is_not_stolen_and_expired_retry_changes_mode(domain_connection, worker_case):
    conn, case = domain_connection, worker_case
    job = sealed_jobs(conn, case)["choice"]
    repo = GradingJobRepository(conn)
    leased = repo.claim(case["user"], job["id"], owner="other-worker", now=NOW, lease_for=timedelta(seconds=10))
    assert not repo.submission_retry(case["user"], job["id"], revision=1, now=NOW, mode="saved")
    assert repo.get(case["user"], job["id"]) == leased
    assert repo.submission_retry(case["user"], job["id"], revision=1, now=NOW + timedelta(seconds=10), mode="saved")
    fresh = repo.get(case["user"], job["id"])
    assert fresh["execution_mode"] == "saved" and fresh["lease_token"] is None


@pytest.mark.parametrize("guard", ["unsealed", "snapshot", "revision", "foreign"])
def test_retry_requires_owned_current_sealed_unfinished_set(domain_connection, worker_case, guard):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, kind="choice") if guard == "unsealed" else sealed_jobs(conn, case)["choice"]
    if guard == "snapshot":
        conn.execute(update(t.quiz_submission_states).where(t.quiz_submission_states.c.quiz_session_id == case["quiz"])
            .values(result_snapshot={"immutable": True}, result_provenance="submitted",
                    result_schema_version="1", result_captured_at=NOW))
    user_id = "00000000-0000-0000-0000-000000000001" if guard == "foreign" else case["user"]
    before = conn.execute(select(t.quiz_grading_jobs)).all()
    with pytest.raises(ApiError):
        GradingJobRepository(conn).submission_retry(user_id, job["id"], revision=2 if guard == "revision" else 1,
                                                   now=NOW, mode="saved", stale_succeeded=True)
    assert conn.execute(select(t.quiz_grading_jobs)).all() == before
