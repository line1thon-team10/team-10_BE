from datetime import timedelta
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text, update

from app.ai.gateway import AiGateway, CleanupFailed, ExecutionResult, FunctionRef, ProviderSpec, parse_json
from app.ai.settings import AiSettings
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services.grading import GradingBudget, GradingService, capture_snapshot, validated_completed_result
from test_grading_adapter import grade
from test_quiz_storage import DAY, NOW, make_case, save


FAKE_PROVIDER = ProviderSpec(FunctionRef("test_grading_adapter", "fake_provider"))


@pytest.fixture
def worker_engine(domain_connection):
    conn = domain_connection
    args = ({"options": f"-csearch_path={conn.scalar(text('select current_schema()'))}"}
            if conn.dialect.name == "postgresql" else {"check_same_thread": False})
    conn.commit()
    engine = create_engine(conn.engine.url, connect_args=args)
    if conn.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi, _):
            dbapi.execute("PRAGMA foreign_keys=ON")
            dbapi.execute("PRAGMA busy_timeout=50")
    try:
        yield engine
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


@pytest.fixture
def worker_case(domain_connection):
    conn = domain_connection
    case = make_case(conn)
    conn.execute(update(t.materials).where(t.materials.c.id == case["material"]).values(page_count=20,
        analysis_status="succeeded", quiz_enabled=True))
    conn.execute(update(t.units).where(t.units.c.id == case["unit"]).values(start_page=1, end_page=10))
    case["source"] = str(uuid4())
    conn.execute(t.question_sources.insert().values(id=case["source"], question_id=case["essay"],
        material_id=case["material"], start_page=2, end_page=3, quote="실제 근거 원문입니다."))
    case["short"], case["short_item"] = str(uuid4()), str(uuid4())
    conn.execute(t.questions.insert().values(id=case["short"], unit_id=case["unit"], type="short", difficulty="low",
        prompt="문자열을 답하세요.", correct_answer="Hello World!", created_at=NOW))
    conn.execute(t.quiz_items.insert().values(id=case["short_item"], quiz_session_id=case["quiz"],
        question_id=case["short"], sort_order=3, inclusion_reason="practice"))
    conn.commit()
    return case


def queue(conn, case, kind="essay", *, mode=None, **changes):
    answer = save(QuizAnswerRepository(conn), case, kind=kind, **changes).answer
    job = GradingJobRepository(conn).enqueue(case["user"], case["quiz"], case[f"{kind}_item"], revision=answer["revision"], now=NOW, mode=mode)
    conn.commit()
    return job


def job_row(conn, job):
    return dict(conn.execute(select(t.quiz_grading_jobs).where(t.quiz_grading_jobs.c.id == job["id"])).mappings().one())


def service(engine, **changes):
    return GradingService(engine, **{"owner": "worker-a", "clock": lambda: NOW + timedelta(seconds=1), **changes})


class LocalExecutor:
    """Transaction interleaving fixture, not a provider/usage measurement."""
    def __init__(self, engine, *, hook=None, score=85, failure=False, deadlines=None):
        self.engine, self.hook, self.score, self.failure, self.deadlines = engine, hook, score, failure, deadlines

    def execute(self, identity, *, provider, validator, deadline, cleanup_timeout, cached_json=None):
        assert self.engine.pool.checkedout() == 0
        if self.deadlines is not None:
            self.deadlines.append(deadline)
        if self.hook:
            self.hook(identity)
        if self.failure or time.monotonic() >= deadline:
            return ExecutionResult(None, (), "failed")
        value = parse_json(cached_json) if cached_json is not None else grade(self.score)
        return ExecutionResult(validator.resolve()(value, parse_json(identity.validation_json)), (), None)


def gateway_factory(**options):
    def factory(engine, **kwargs):
        gateway = AiGateway(engine, **kwargs)
        gateway.executor = LocalExecutor(engine, **options)
        return gateway
    return factory


def no_gateway(*args, **kwargs):
    pytest.fail("Deterministic grading must not initialize a gateway or credentials")


@pytest.mark.parametrize("kind,answer,expected,score", [
    ("choice", 0, True, None), ("choice", 1, False, None),
    ("short", "  hELLo\tWORLD!\n", True, None), ("short", "Hello World", False, None),
    ("short", "HelloWorld!", False, None), ("short", "", False, None),
    ("essay", " \t\n", False, 0),
])
def test_deterministic_grading_never_calls_ai_or_finalizes_scores(domain_connection, worker_engine, worker_case, kind, answer, expected, score):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, kind=kind, **{"choice" if kind == "choice" else "text": answer})
    outcome = service(worker_engine, gateway_factory=no_gateway).run_job(case["user"], job["id"])
    assert outcome.state == "succeeded"
    result = job_row(conn, job)["result"]
    assert result["is_correct"] is expected and result["essay_score"] == score and result["grading_status"] == "graded"
    assert result["provenance"]["saved_result_id"] is None
    assert conn.execute(select(t.quiz_attempts)).first() is None and conn.execute(select(t.mastery_history)).first() is None
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    assert conn.scalar(select(t.quiz_sessions.c.status)) == "ready"


@pytest.mark.parametrize("score,correct", [(0, False), (84, False), (85, True), (100, True)])
def test_essay_boundaries_and_completed_result_revalidation(domain_connection, worker_engine, worker_case, score, correct):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    outcome = service(worker_engine, gateway_factory=gateway_factory(score=score)).run_job(case["user"], job["id"])
    assert outcome.state == "succeeded" and outcome.result["is_correct"] is correct and outcome.result["essay_score"] == score
    with worker_engine.begin() as transaction:
        assert validated_completed_result(transaction, case["user"], job["id"]) == outcome.result
    conn.execute(update(t.questions).where(t.questions.c.id == case["essay"]).values(correct_answer="수정된 모범답안"))
    conn.commit()
    with worker_engine.begin() as transaction:
        assert validated_completed_result(transaction, case["user"], job["id"]) is None


def test_claim_and_snapshot_are_committed_without_any_lock_during_external_work(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    def outside(identity):
        with worker_engine.begin() as transaction:
            repo = GradingJobRepository(transaction)
            running = repo.get(case["user"], job["id"])
            assert running["status"] == "running" and running["claim_count"] == 1
            repo.answers.lock_quiz(case["user"], case["quiz"])
            assert repo.claim(case["user"], job["id"], owner="worker-b", now=NOW + timedelta(seconds=2),
                              lease_for=timedelta(seconds=30)) is None
            snapshot = capture_snapshot(transaction, case["user"], running)
            edited = snapshot.data
            edited["question"]["prompt"] = "mutated outside"
            assert snapshot.data["question"]["prompt"] == "essay"
    assert service(worker_engine, gateway_factory=gateway_factory(hook=outside)).run_job(case["user"], job["id"]).state == "succeeded"
    assert job_row(conn, job)["claim_count"] == 1


@pytest.mark.parametrize("target", ["answer", "question", "source", "source_added", "material", "unit"])
def test_late_changed_input_cannot_adopt_stale_result(domain_connection, worker_engine, worker_case, target):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    def change(_):
        with worker_engine.begin() as transaction:
            if target == "answer":
                save(QuizAnswerRepository(transaction), case, revision=1, text="새 답안")
            elif target == "question":
                transaction.execute(update(t.questions).where(t.questions.c.id == case["essay"]).values(prompt="새 문항"))
            elif target == "source":
                transaction.execute(update(t.question_sources).where(t.question_sources.c.id == case["source"]).values(quote="새 근거"))
            elif target == "source_added":
                transaction.execute(t.question_sources.insert().values(id=str(uuid4()), question_id=case["essay"],
                    material_id=case["material"], start_page=4, end_page=4, quote="추가 근거"))
            elif target == "material":
                transaction.execute(update(t.materials).where(t.materials.c.id == case["material"]).values(analysis_version="v2"))
            else:
                transaction.execute(update(t.units).where(t.units.c.id == case["unit"]).values(end_page=12))
    outcome = service(worker_engine, gateway_factory=gateway_factory(hook=change)).run_job(case["user"], job["id"])
    row = job_row(conn, job)
    assert row["result"] is None
    assert (outcome.state, row["status"]) == (("stale", "cancelled") if target == "answer" else ("failed", "failed"))
    if target != "answer":
        assert row["error_code"] == "GRADING_INPUT_CHANGED"
    assert conn.execute(select(t.quiz_attempts)).first() is None


def test_expired_lease_is_reclaimed_after_crash_and_late_token_cannot_finish(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    lease = GradingJobRepository(conn).claim(case["user"], job["id"], owner="crashed", now=NOW, lease_for=timedelta(seconds=1))
    conn.commit()
    outcome = service(worker_engine, clock=lambda: NOW + timedelta(seconds=2), gateway_factory=gateway_factory()).run_next()
    assert outcome.state == "succeeded"
    row = job_row(conn, job)
    assert row["claim_count"] == 2
    assert not GradingJobRepository(conn).complete(case["user"], job["id"], owner="crashed", token=lease["lease_token"],
        revision=1, now=NOW + timedelta(seconds=3), result={"is_correct": False, "essay_score": 0})
    assert job_row(conn, job)["result"]["essay_score"] == 85


def test_worker_that_outlives_lease_cannot_commit_result(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    now = [NOW]
    def advance(_):
        now[0] = NOW + timedelta(minutes=1)
    outcome = service(worker_engine, clock=lambda: now[0], gateway_factory=gateway_factory(hook=advance)).run_job(case["user"], job["id"])
    assert outcome.state == "stale" and job_row(conn, job)["result"] is None
    assert job_row(conn, job)["status"] == "running"


def test_missing_key_fails_once_and_automatic_poll_never_retries_failed_job(domain_connection, worker_engine, worker_case, monkeypatch):
    conn, case = domain_connection, worker_case
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    job = queue(conn, case)
    worker = service(worker_engine)
    assert worker.run_next().state == "failed"
    row = job_row(conn, job)
    assert row["error_code"] == "AI_FAILED" and row["result"] is None
    conn.rollback()
    for _ in range(3):
        assert worker.run_next().state == "idle"
        assert worker.run_job(case["user"], job["id"]).state == "not_claimed"
    assert job_row(conn, job)["claim_count"] == 1
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    conn.rollback()
    assert service(worker_engine, gateway_factory=gateway_factory()).run_job(case["user"], job["id"], retry_failed=True).state == "succeeded"
    assert job_row(conn, job)["claim_count"] == 2


@pytest.mark.parametrize("mode", ["saved", "backup"])
def test_saved_mode_miss_cannot_initialize_provider_or_credentials(domain_connection, worker_engine, worker_case, monkeypatch, mode):
    conn, case = domain_connection, worker_case
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    job = queue(conn, case, mode=mode)
    forbidden = ProviderSpec(FunctionRef("module_that_must_not_be_imported", "provider"))
    outcome = service(worker_engine, provider=forbidden).run_job(case["user"], job["id"], mode=mode)
    assert outcome.state == "failed" and job_row(conn, job)["result"] is None
    assert conn.execute(select(t.ai_usage_logs)).first() is None and conn.execute(select(t.ai_saved_results)).first() is None


def test_real_gateway_fake_transport_records_null_ai_job_and_keyless_cache_reuse(domain_connection, worker_engine, worker_case, monkeypatch):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, text="valid")
    result = service(worker_engine, provider=FAKE_PROVIDER).run_job(case["user"], job["id"])
    assert result.state == "succeeded"
    usage = conn.execute(select(t.ai_usage_logs)).mappings().one()
    assert usage["job_id"] is None and usage["user_id"] == case["user"] and usage["kind"] == "essay_grade"
    assert usage["input_tokens"] is None and usage["estimated_cost"] is None
    # A different set containing the same original question and exact input may reuse the cache.
    second = {**case, "quiz": str(uuid4()), "essay_item": str(uuid4())}
    conn.execute(t.quiz_sessions.insert().values(id=second["quiz"], user_id=case["user"], subject_id=case["subject"],
        type="practice", quiz_date=DAY, status="ready", created_at=NOW))
    conn.execute(t.quiz_items.insert().values(id=second["essay_item"], quiz_session_id=second["quiz"],
        question_id=case["essay"], sort_order=1, inclusion_reason="practice"))
    new_job = queue(conn, second, text="valid", mode="saved")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cached = service(worker_engine, provider=ProviderSpec(FunctionRef("do_not_import_this", "provider"))).run_job(
        case["user"], new_job["id"], mode="saved")
    assert cached.state == "succeeded" and cached.result["grading_status"] == "fallback"
    assert cached.result["provenance"]["saved_result_id"] == result.result["provenance"]["saved_result_id"]
    assert len(conn.execute(select(t.ai_usage_logs)).all()) == 1


@pytest.mark.parametrize("change", ["foreign_source", "bad_page", "missing_quote", "duplicate_correct_choice"])
def test_invalid_owned_snapshot_fails_without_external_execution(domain_connection, worker_engine, worker_case, change):
    conn, case = domain_connection, worker_case
    kind = "choice" if change == "duplicate_correct_choice" else "essay"
    job = queue(conn, case, kind=kind)
    if change == "foreign_source":
        other = make_case(conn)
        conn.execute(update(t.question_sources).where(t.question_sources.c.id == case["source"]).values(material_id=other["material"]))
    elif change == "bad_page":
        conn.execute(update(t.question_sources).where(t.question_sources.c.id == case["source"]).values(end_page=21))
    elif change == "missing_quote":
        conn.execute(update(t.question_sources).where(t.question_sources.c.id == case["source"]).values(quote=None))
    else:
        conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(choices=[{"text": "맞음"}, {"text": "맞음"}]))
    conn.commit()
    outcome = service(worker_engine, gateway_factory=no_gateway).run_job(case["user"], job["id"])
    assert outcome.state == "failed" and outcome.error_code == "INVALID_GRADING_INPUT"
    assert job_row(conn, job)["result"] is None


def test_foreign_owner_cannot_claim_job(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    other = make_case(conn)
    with pytest.raises(ApiError) as error:
        service(worker_engine, gateway_factory=no_gateway).run_job(other["user"], job["id"])
    assert error.value.code == "NO_QUIZ" and error.value.status == 404
    assert job_row(conn, job)["status"] == "queued"


def test_completion_rollback_leaves_committed_claim_and_answer_for_recovery(domain_connection, worker_engine, worker_case, monkeypatch):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    original = GradingJobRepository.complete
    def fail_after_update(self, *args, **kwargs):
        assert original(self, *args, **kwargs)
        raise RuntimeError("simulated commit-path crash")
    monkeypatch.setattr(GradingJobRepository, "complete", fail_after_update)
    with pytest.raises(RuntimeError, match="commit-path"):
        service(worker_engine, gateway_factory=gateway_factory()).run_job(case["user"], job["id"])
    row = job_row(conn, job)
    assert row["status"] == "running" and row["result"] is None
    assert conn.scalar(select(t.quiz_answers.c.text)) == "  원문  답안\n"
    assert conn.execute(select(t.quiz_attempts)).first() is None


def test_cleanup_failure_is_not_hidden_as_normal_ai_failure(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    def fail(_):
        raise CleanupFailed("simulated unreaped child")
    with pytest.raises(CleanupFailed):
        service(worker_engine, gateway_factory=gateway_factory(hook=fail)).run_job(case["user"], job["id"])
    assert job_row(conn, job)["status"] == "running" and job_row(conn, job)["result"] is None


def test_two_items_share_original_deadline_after_slow_first_item(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    first = queue(conn, case)
    second_case = {**case, "quiz": str(uuid4()), "essay_item": str(uuid4())}
    conn.execute(t.quiz_sessions.insert().values(id=second_case["quiz"], user_id=case["user"], subject_id=case["subject"],
        type="practice", quiz_date=DAY, status="ready", created_at=NOW))
    conn.execute(t.quiz_items.insert().values(id=second_case["essay_item"], quiz_session_id=second_case["quiz"],
        question_id=case["essay"], sort_order=1, inclusion_reason="practice"))
    second = queue(conn, second_case, text="두 번째 답")
    deadlines, remaining = [], []
    def slow_first(_):
        remaining.append(budget.remaining)
        if len(remaining) == 1:
            time.sleep(0.2)
    budget = GradingBudget.start(1.5)
    worker = service(worker_engine, gateway_factory=gateway_factory(hook=slow_first, deadlines=deadlines))
    assert worker.run_job(case["user"], first["id"], budget=budget).state == "succeeded"
    assert worker.run_job(case["user"], second["id"], budget=budget).state == "succeeded"
    assert deadlines == [budget.deadline, budget.deadline]
    assert remaining[1] < remaining[0] - 0.18


def test_exhausted_shared_budget_never_claims_another_job(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    assert service(worker_engine, gateway_factory=no_gateway).run_job(case["user"], job["id"],
        budget=GradingBudget(time.monotonic() - 1)).state == "budget_exhausted"
    assert job_row(conn, job)["status"] == "queued" and job_row(conn, job)["claim_count"] == 0


def test_legacy_succeeded_result_without_provenance_is_not_assumed_current(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    job = queue(conn, case)
    repo = GradingJobRepository(conn)
    lease = repo.claim(case["user"], job["id"], owner="legacy", now=NOW, lease_for=timedelta(seconds=10))
    assert repo.complete(case["user"], job["id"], owner="legacy", token=lease["lease_token"], revision=1,
        now=NOW + timedelta(seconds=1), result={"is_correct": True, "essay_score": 85})
    conn.commit()
    with worker_engine.begin() as transaction:
        assert validated_completed_result(transaction, case["user"], job["id"]) is None
