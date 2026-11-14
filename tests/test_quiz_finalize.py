from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services import quiz_finalization as finalization
from app.services.grading import GradingService
from app.services.quiz import QuizService
from tests.test_quiz_answer_api import DAY, answer_case, make_case, put, snapshot
from test_grading_worker import gateway_factory


def bulk(case, *, text="", saved=False):
    return {"quizId": case["quiz"], "savedGrading": saved, "answers": [
        {"questionId": case[f"{kind}_item"], "confidence": "sure",
         **({"choice": 0} if kind in ("choice", "ox") else {"text": text if kind == "essay" else "맞음"})}
        for kind in ("choice", "ox", "short", "essay")]}


def submit(client, case, **changes):
    return client.post(f"/api/quiz/{case['quiz']}/submit", headers={"X-Anonymous-Id": case["user"]},
                       json=bulk(case, **changes))


def get_result(client, case):
    return client.get(f"/api/quiz/{case['quiz']}/result", headers={"X-Anonymous-Id": case["user"]})


def use_gateway(monkeypatch, **options):
    monkeypatch.setattr(finalization, "GradingService", lambda engine, **kwargs:
        GradingService(engine, gateway_factory=gateway_factory(**options), **kwargs))


def facts(conn, case):
    return {"attempts": conn.execute(select(t.quiz_attempts).join(t.quiz_items,
                t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id).where(
                    t.quiz_items.c.quiz_session_id == case["quiz"])).all(),
            "history": conn.execute(select(t.mastery_history).where(
                t.mastery_history.c.quiz_session_id == case["quiz"])).all()}


def test_final_submit_commits_once_and_replays_immutable_result(api_client, answer_case, domain_connection):
    case, conn = answer_case, domain_connection
    assert get_result(api_client, case).status_code == 404
    response = submit(api_client, case)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["data"]["summary"] == {"total": 4, "correct": 3, "sureCorrect": 3, "unsureCorrect": 0, "wrong": 1}
    before = snapshot(conn)
    assert len(facts(conn, case)["attempts"]) == 4 and len(facts(conn, case)["history"]) == 1
    state = QuizAnswerRepository(conn).submission(case["user"], case["quiz"])
    assert state["result_provenance"] == "submitted" and state["result_snapshot"] == result["data"]
    conn.commit()
    assert submit(api_client, case).json() == result
    assert get_result(api_client, case).json() == result
    assert snapshot(conn) == before
    conn.execute(update(t.users).where(t.users.c.id == case["user"]).values(demo_date=DAY + timedelta(days=10)))
    conn.execute(update(t.units).where(t.units.c.id == case["unit"]).values(mastery_score=1, title="바뀐 단원"))
    conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(prompt="바뀐 문제"))
    conn.commit()
    assert get_result(api_client, case).json() == result
    assert submit(api_client, case).json() == result
    changed = submit(api_client, case, text="다른 입력")
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "QUIZ_FINALIZED"


def test_failure_keeps_sealed_answers_and_partial_jobs_without_public_scores(
        api_client, answer_case, domain_connection, monkeypatch):
    case, conn = answer_case, domain_connection
    use_gateway(monkeypatch, failure=True)
    failed = submit(api_client, case, text="보존할 원문")
    assert failed.status_code == 503, failed.text
    assert get_result(api_client, case).status_code == 503
    assert facts(conn, case) == {"attempts": [], "history": []}
    answers = QuizAnswerRepository(conn).list_current(case["user"], case["quiz"])
    assert len(answers) == 4 and next(a for a in answers if a["quiz_item_id"] == case["essay_item"])["text"] == "보존할 원문"
    assert sorted(conn.scalars(select(t.quiz_grading_jobs.c.status))) == ["failed", "succeeded", "succeeded", "succeeded"]
    conn.commit()
    assert put(api_client, case, revision=1, text="변경").status_code == 409
    use_gateway(monkeypatch, score=85)
    retried = submit(api_client, case, text="보존할 원문")
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["summary"]["correct"] == 4
    assert QuizAnswerRepository(conn).list_current(case["user"], case["quiz"]) == answers
    assert len(facts(conn, case)["attempts"]) == 4 and len(facts(conn, case)["history"]) == 1


def test_external_grading_has_no_request_connection_or_user_lock(api_client, answer_case, domain_connection, monkeypatch):
    case = answer_case
    observed = []
    def outside(identity):
        from app.db.session import engine
        assert engine.pool.checkedout() == 0
        with engine.begin() as conn:
            repo = QuizAnswerRepository(conn)
            repo.lock_quiz(case["user"], case["quiz"])
            assert repo.submission(case["user"], case["quiz"])["result_snapshot"] is None
            assert len(repo.list_current(case["user"], case["quiz"])) == 4
        observed.append(True)
    use_gateway(monkeypatch, hook=outside, score=85)
    response = submit(api_client, case, text="채점 요청")
    # This also exercises get_conn's context exit after the auth connection was
    # closed. A deferred commit failure must not turn the request into a 500.
    assert response.status_code == 200, response.text
    assert observed == [True] and get_result(api_client, case).json() == response.json()


@pytest.mark.parametrize("boundary", ["mastery", "snapshot"])
def test_finalization_failure_rolls_back_all_score_facts_but_keeps_inputs_and_grades(
        api_client, answer_case, domain_connection, monkeypatch, boundary):
    case, conn = answer_case, domain_connection
    owner, method = (QuizService, "_recalc_mastery") if boundary == "mastery" else (QuizAnswerRepository, "store_result_snapshot")
    original = getattr(owner, method)
    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected finalization failure")
    monkeypatch.setattr(owner, method, fail_after_write)
    response = submit(api_client, case)
    assert response.status_code == 500, response.text
    assert facts(conn, case) == {"attempts": [], "history": []}
    assert conn.execute(select(t.weak_questions)).first() is None
    assert QuizAnswerRepository(conn).submission(case["user"], case["quiz"])["result_snapshot"] is None
    assert list(conn.scalars(select(t.quiz_grading_jobs.c.status))) == ["succeeded"] * 4
    conn.commit()
    monkeypatch.setattr(owner, method, original)
    assert submit(api_client, case).status_code == 200
    assert len(facts(conn, case)["attempts"]) == 4


def test_saved_request_pins_existing_live_jobs_and_miss_never_calls_provider(
        api_client, answer_case, domain_connection, monkeypatch):
    from app.ai.gateway import FunctionRef, ProviderSpec
    case, conn = answer_case, domain_connection
    assert put(api_client, case, text="캐시 없는 답안").status_code == 200
    monkeypatch.setattr(finalization, "GradingService", lambda engine, **kwargs:
        GradingService(engine, provider=ProviderSpec(FunctionRef("must_not_import_provider", "forbidden")), **kwargs))
    response = submit(api_client, case, text="캐시 없는 답안", saved=True)
    assert response.status_code == 503, response.text
    assert set(conn.scalars(select(t.quiz_grading_jobs.c.execution_mode))) == {"saved"}
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    assert facts(conn, case) == {"attempts": [], "history": []}
    assert get_result(api_client, case).status_code == 503


def test_foreign_submit_and_duplicate_items_never_seal(api_client, answer_case, domain_connection):
    case, conn = answer_case, domain_connection
    other = make_case(conn)
    response = api_client.post(f"/api/quiz/{case['quiz']}/submit", headers={"X-Anonymous-Id": other["user"]}, json=bulk(case))
    assert response.status_code == 404
    body = bulk(case)
    body["answers"][-1] = deepcopy(body["answers"][0])
    response = api_client.post(f"/api/quiz/{case['quiz']}/submit", headers={"X-Anonymous-Id": case["user"]}, json=body)
    assert response.status_code == 422
    assert conn.execute(select(t.quiz_submission_states)).first() is None
    assert conn.execute(select(t.quiz_answers)).first() is None
