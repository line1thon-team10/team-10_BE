"""Foreign item damage must not permanently hide another user's healthy work."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.settings import AiSettings
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services.grading import GradingBudget
from test_grading_worker import no_gateway, queue, service, worker_case, worker_engine
from test_quiz_storage import DAY, NOW, make_case, save


def test_mixed_foreign_quiz_cannot_starve_healthy_job_beyond_poll_limit(domain_connection, worker_engine, worker_case):
    conn, bad = domain_connection, worker_case
    answers, jobs = QuizAnswerRepository(conn), GradingJobRepository(conn)
    for index in range(100):
        qid, itemid = str(uuid4()), str(uuid4())
        conn.execute(t.questions.insert().values(id=qid, unit_id=bad['unit'], code=f'poll-{index}', type='choice',
            difficulty='low', prompt='정상 문항', choices=[{'text': 'A'}, {'text': 'B'}], correct_answer='A', created_at=NOW))
        conn.execute(t.quiz_items.insert().values(id=itemid, quiz_session_id=bad['quiz'], question_id=qid,
            sort_order=10 + index, inclusion_reason='practice'))
        answer = answers.save(bad['user'], bad['quiz'], itemid, revision=0, choice=0, confidence='sure', now=NOW, answered_date=DAY)
        jobs.enqueue(bad['user'], bad['quiz'], itemid, revision=answer.answer['revision'], now=NOW - timedelta(minutes=1))
    conn.commit()
    healthy = make_case(conn)
    answer = save(answers, healthy, kind='choice').answer
    tail = jobs.enqueue(healthy['user'], healthy['quiz'], healthy['choice_item'], revision=answer['revision'], now=NOW)
    # Existing queued items remain owned, but their sibling now points at another
    # user's question. pending() accepts them; item() rejects the whole quiz.
    conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == bad['essay_item']).values(question_id=healthy['essay']))
    conn.commit()
    worker = service(worker_engine, gateway_factory=no_gateway)
    observed = [worker.run_next().state for _ in range(3)]
    status = conn.scalar(select(t.quiz_grading_jobs.c.status).where(t.quiz_grading_jobs.c.id == tail['id']))
    assert status == 'succeeded', {'poll_results': observed, 'healthy_status': status,
        'queued_jobs': len(conn.scalars(select(t.quiz_grading_jobs.c.id).where(t.quiz_grading_jobs.c.status == 'queued')).all())}


def healthy_tail(conn):
    case = make_case(conn)
    answer = save(QuizAnswerRepository(conn), case, kind="choice").answer
    job = GradingJobRepository(conn).enqueue(case["user"], case["quiz"], case["choice_item"],
        revision=answer["revision"], now=NOW + timedelta(seconds=1))
    conn.commit()
    return case, job


def test_shared_whole_quiz_validation_is_cached_across_keyset_pages(domain_connection, worker_engine, worker_case, monkeypatch):
    conn, bad = domain_connection, worker_case
    first = queue(conn, bad, kind="choice")
    second = queue(conn, bad, kind="essay")
    healthy, tail = healthy_tail(conn)
    calls = []
    original_items, original_pending = QuizAnswerRepository.items, GradingJobRepository.pending
    def shared_validation(self, user_id, quiz_id):
        calls.append(quiz_id)
        rows = original_items(self, user_id, quiz_id)
        if quiz_id == bad["quiz"]:
            # Future shared checks, including Q-03's concept/unit validation,
            # must affect polling without duplicating their SQL in this worker.
            raise ApiError("NO_QUIZ", "invalid concept/unit relationship", 404)
        return rows
    pages = []
    def one_at_a_time(self, **kwargs):
        assert kwargs.get("limit", 100) == 100
        page = original_pending(self, **{**kwargs, "limit": 1})
        pages.extend(row["id"] for row in page)
        return page
    monkeypatch.setattr(QuizAnswerRepository, "items", shared_validation)
    monkeypatch.setattr(GradingJobRepository, "pending", one_at_a_time)
    worker = service(worker_engine, gateway_factory=no_gateway)
    assert worker.run_next().job_id == tail["id"]
    assert pages == [*sorted((first["id"], second["id"])), tail["id"]]
    assert calls.count(bad["quiz"]) == 1
    assert conn.scalar(select(t.quiz_grading_jobs.c.status).where(t.quiz_grading_jobs.c.id == tail["id"])) == "succeeded"
    assert conn.execute(select(t.ai_usage_logs)).first() is None


def simulated_budget(monkeypatch):
    elapsed = [0.0]
    class Budget(GradingBudget):
        @property
        def remaining(self):
            return max(0.0, self.deadline - elapsed[0])
    monkeypatch.setattr(GradingBudget, "start", classmethod(lambda cls, seconds=20: Budget(elapsed[0] + seconds)))
    return elapsed


@pytest.mark.parametrize("interrupt", ["budget", "stop"])
def test_interrupted_scan_resumes_after_discarded_candidate(domain_connection, worker_engine, worker_case, monkeypatch, interrupt):
    conn, bad = domain_connection, worker_case
    queue(conn, bad, kind="choice")
    healthy, tail = healthy_tail(conn)
    conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == bad["essay_item"]).values(question_id=healthy["essay"]))
    conn.commit()
    elapsed, stopped, visits = simulated_budget(monkeypatch), [False], []
    original = QuizAnswerRepository.items
    def checking(self, user_id, quiz_id):
        visits.append(quiz_id)
        if quiz_id == bad["quiz"]:
            elapsed[0] += 2 if interrupt == "budget" else 0
            stopped[0] = interrupt == "stop"
        return original(self, user_id, quiz_id)
    monkeypatch.setattr(QuizAnswerRepository, "items", checking)
    worker = service(worker_engine, settings=AiSettings(request_budget_seconds=1), gateway_factory=no_gateway)
    assert worker.run_next(stop_requested=lambda: stopped[0]).state == ("budget_exhausted" if interrupt == "budget" else "idle")
    assert visits == [bad["quiz"]]
    assert conn.scalar(select(t.quiz_grading_jobs.c.claim_count).where(t.quiz_grading_jobs.c.id == tail["id"])) == 0
    conn.rollback()
    stopped[0] = False
    assert worker.run_next(stop_requested=lambda: stopped[0]).state == "succeeded"
    assert visits.count(bad["quiz"]) == 1
    assert conn.scalar(select(t.quiz_grading_jobs.c.status).where(t.quiz_grading_jobs.c.id == tail["id"])) == "succeeded"


def test_budget_spent_validating_healthy_candidate_does_not_skip_it(domain_connection, worker_engine, worker_case, monkeypatch):
    conn, case = domain_connection, worker_case
    job = queue(conn, case, kind="choice")
    elapsed = simulated_budget(monkeypatch)
    original, visits = QuizAnswerRepository.items, []
    def slow_once(self, user_id, quiz_id):
        if not visits:
            elapsed[0] += 2
        visits.append(quiz_id)
        return original(self, user_id, quiz_id)
    monkeypatch.setattr(QuizAnswerRepository, "items", slow_once)
    worker = service(worker_engine, settings=AiSettings(request_budget_seconds=1), gateway_factory=no_gateway)
    assert worker.run_next().state == "budget_exhausted"
    assert conn.scalar(select(t.quiz_grading_jobs.c.claim_count)) == 0
    conn.rollback()
    result = worker.run_next()
    assert result.state == "succeeded" and result.job_id == job["id"]


def test_cursor_wraps_for_new_work_inserted_before_last_position(domain_connection, worker_engine, worker_case):
    conn, case = domain_connection, worker_case
    first = queue(conn, case, kind="choice")
    worker = service(worker_engine, gateway_factory=no_gateway)
    assert worker.run_next().job_id == first["id"]
    _, earlier = healthy_tail(conn)
    conn.execute(update(t.quiz_grading_jobs).where(t.quiz_grading_jobs.c.id == earlier["id"]).values(created_at=NOW - timedelta(days=1)))
    conn.commit()
    result = worker.run_next()
    assert result.state == "succeeded" and result.job_id == earlier["id"]
    assert worker.run_next().state == "idle"


def test_unexpected_validation_failure_is_not_silently_skipped(domain_connection, worker_engine, worker_case, monkeypatch):
    queue(domain_connection, worker_case, kind="choice")
    def broken(*args):
        raise RuntimeError("database/validation implementation failure")
    monkeypatch.setattr(QuizAnswerRepository, "items", broken)
    with pytest.raises(RuntimeError, match="implementation failure"):
        service(worker_engine, gateway_factory=no_gateway).run_next()
    assert domain_connection.scalar(select(t.quiz_grading_jobs.c.claim_count)) == 0


def append_job(conn, at):
    case = make_case(conn)
    answer = QuizAnswerRepository(conn).save(case["user"], case["quiz"], case["choice_item"],
        revision=0, choice=0, confidence="sure", now=at, answered_date=DAY).answer
    job = GradingJobRepository(conn).enqueue(case["user"], case["quiz"], case["choice_item"],
        revision=answer["revision"], now=at)
    conn.commit()
    return case, job


@pytest.mark.parametrize("reappears", ["expired_lease", "repaired_quiz"])
def test_old_eligible_job_is_not_starved_by_a_continuing_trickle_of_new_jobs(
        domain_connection, worker_engine, reappears):
    conn = domain_connection
    old_case, old_job = append_job(conn, NOW)
    next_case, first_new_job = append_job(conn, NOW + timedelta(seconds=1))
    if reappears == "expired_lease":
        claim = GradingJobRepository(conn).claim(old_case["user"], old_job["id"], owner="crashed-worker",
            now=NOW, lease_for=timedelta(seconds=5))
        assert claim is not None
    else:
        conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == old_case["essay_item"])
            .values(question_id=next_case["essay"]))
    conn.commit()
    instant = [NOW + timedelta(seconds=2)]
    worker = service(worker_engine, clock=lambda: instant[0], gateway_factory=no_gateway)
    assert worker.run_next().job_id == first_new_job["id"]
    # The old job now becomes eligible *behind* this same worker's cursor.
    instant[0] = NOW + timedelta(seconds=10)
    if reappears == "repaired_quiz":
        conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == old_case["essay_item"])
            .values(question_id=old_case["essay"]))
        conn.commit()
    observed = []
    for index in range(12):
        instant[0] += timedelta(seconds=1)
        _, incoming = append_job(conn, instant[0])
        result = worker.run_next()
        observed.append({"state": str(result.state), "old_job_chosen": result.job_id == old_job["id"],
                         "new_job_chosen": result.job_id == incoming["id"]})
        state = conn.execute(select(t.quiz_grading_jobs).where(
            t.quiz_grading_jobs.c.id == old_job["id"])).mappings().one()
        conn.rollback()
        if state["status"] == "succeeded":
            break
    # The witness extends to any number of polls: appending one newer job is
    # enough to keep pending(after=cursor) nonempty, so the old eligible job
    # receives no claim while every newer job is processed.
    assert state["status"] == "succeeded", {
        "reappears": reappears, "old_status": state["status"], "old_claim_count": state["claim_count"],
        "polls": observed, "cursor": worker._pending_cursor,
    }
