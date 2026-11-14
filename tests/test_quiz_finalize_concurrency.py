"""Close the preserved legacy-race reproduction at the new external boundary.

Original: /private/tmp/team10-quiz-answer-legacy-race/test_legacy_quiz_submit_race.py
That file stays unchanged; _grade_essay is no longer the HTTP execution path.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import select, update

from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services.grading import GradingService
from tests.test_quiz_answer_api import answer_case, put
from tests.test_quiz_answer_api_concurrency import fast_lock_failure
from tests.test_quiz_finalize import facts, get_result, submit, use_gateway


def test_http_submit_first_seals_before_external_call_and_serializes_duplicate_submit(
        api_client, answer_case, domain_connection, monkeypatch):
    case, conn = answer_case, domain_connection
    entered, release = Event(), Event()
    def outside(identity):
        entered.set()
        assert release.wait(10)
    use_gateway(monkeypatch, hook=outside, score=85)
    with ThreadPoolExecutor(max_workers=1) as pool:
        submitting = pool.submit(submit, api_client, case, text="제출한 원문")
        try:
            assert entered.wait(10)
            changed = put(api_client, case, revision=1, text="제출 도중 바뀐 답안")
            assert changed.status_code == 409 and changed.json()["error"]["code"] == "QUIZ_FINALIZED"
            duplicate = submit(api_client, case, text="제출한 원문")
            assert duplicate.status_code == 503 and duplicate.json()["error"]["code"] == "AI_FAILED"
            assert submit(api_client, case, text="다른 제출").status_code == 409
            assert get_result(api_client, case).status_code == 503
            assert facts(conn, case) == {"attempts": [], "history": []}
            conn.rollback()
        finally:
            release.set()
        completed = submitting.result(timeout=10)
    assert completed.status_code == 200, completed.text
    answer = QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"])
    attempt = conn.execute(select(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id == case["essay_item"])).mappings().one()
    assert answer["text"] == attempt["answer"] == "제출한 원문"
    assert len(facts(conn, case)["attempts"]) == 4 and len(facts(conn, case)["history"]) == 1
    conn.commit()
    assert submit(api_client, case, text="제출한 원문").json() == completed.json()


def test_http_put_first_makes_submit_wait_then_seals_the_committed_input(
        api_client, answer_case, domain_connection, fast_lock_failure, monkeypatch):
    case, conn = answer_case, domain_connection
    entered, release = Event(), Event()
    original = GradingJobRepository.enqueue
    def held(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(GradingJobRepository, "enqueue", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        editing = pool.submit(put, api_client, case, text="")
        try:
            assert entered.wait(10)
            response = submit(api_client, case)
            assert response.status_code == 409 and response.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
            assert conn.execute(select(t.quiz_submission_states)).first() is None
            conn.rollback()
        finally:
            release.set()
        assert editing.result(timeout=10).status_code == 200
    monkeypatch.setattr(GradingJobRepository, "enqueue", original)
    saved = QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"])
    conn.commit()
    response = submit(api_client, case)
    assert response.status_code == 200, response.text
    assert QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"]) == saved
    assert len(facts(conn, case)["attempts"]) == 4


def test_only_explicit_submit_retry_regrades_stale_success_before_final_snapshot(
        api_client, answer_case, domain_connection, monkeypatch):
    from app.db.session import engine
    case, conn = answer_case, domain_connection
    assert put(api_client, case, "choice").status_code == 200
    choice_job = conn.scalar(select(t.quiz_grading_jobs.c.id))
    conn.commit()
    assert GradingService(engine, owner="pregrading").run_job(case["user"], choice_job).state == "succeeded"
    def mutate_prior_success(identity):
        with engine.begin() as other:
            other.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(explanation="새 해설"))
    use_gateway(monkeypatch, hook=mutate_prior_success, score=85)
    response = submit(api_client, case, text="최종 답")
    assert response.status_code == 503, response.text
    assert facts(conn, case) == {"attempts": [], "history": []}
    assert conn.scalar(select(t.quiz_grading_jobs.c.claim_count).where(t.quiz_grading_jobs.c.id == choice_job)) == 1
    conn.commit()
    assert get_result(api_client, case).status_code == 503
    assert GradingService(engine, owner="automatic").run_next().state == "idle"
    use_gateway(monkeypatch, score=85)
    completed = submit(api_client, case, text="최종 답")
    assert completed.status_code == 200, completed.text
    assert conn.scalar(select(t.quiz_grading_jobs.c.claim_count).where(t.quiz_grading_jobs.c.id == choice_job)) == 2
    conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(explanation="다시 수정"))
    conn.commit()
    assert submit(api_client, case, text="최종 답").json() == completed.json()
    assert conn.scalar(select(t.quiz_grading_jobs.c.claim_count).where(t.quiz_grading_jobs.c.id == choice_job)) == 2
