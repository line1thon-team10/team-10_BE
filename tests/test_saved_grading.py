from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update

from app.ai.gateway import GatewayRequest, InvalidOutput, MaterialRevision, ProcessExecutor, parse_json
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.user import User
from app.repositories.users import UserRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.seed.essay_examples import DEMO_ESSAY, register_saved_evaluation
from app.services import grading
from tests.test_ai_gateway import NOW, add_material, gateway_user, identity, result_repository


@pytest.mark.parametrize("rollback", [False, True])
def test_seed_result_uses_caller_transaction_without_an_early_commit(
        result_repository, domain_connection, rollback):
    conn = domain_connection
    user = User.anonymous(now=NOW)
    UserRepository(conn).add(user)
    request = GatewayRequest(user.id, identity())
    value = {"examDate": "2026-12-01"}
    result_id = result_repository.save_in_transaction(conn, request, value)
    value["examDate"] = "changed outside the stored result"
    assert conn.in_transaction() and not conn.closed
    assert conn.scalar(select(t.ai_saved_results.c.result).where(t.ai_saved_results.c.id == result_id)) == {
        "examDate": "2026-12-01"}
    with result_repository.engine.connect() as other:
        assert other.scalar(select(t.users.c.id).where(t.users.c.id == user.id)) is None
        assert other.scalar(select(t.ai_saved_results.c.id).where(t.ai_saved_results.c.id == result_id)) is None
    if rollback:
        conn.rollback()
        assert conn.scalar(select(t.users.c.id).where(t.users.c.id == user.id)) is None
        assert conn.scalar(select(t.ai_saved_results.c.id).where(t.ai_saved_results.c.id == result_id)) is None
    else:
        conn.commit()
        saved = result_repository.lookup(request)
        assert saved.id == result_id and saved.source == "private_cache"
        assert saved.result == {"examDate": "2026-12-01"}


def test_transactional_seed_result_refuses_foreign_material_and_invalid_json(
        result_repository, domain_connection, gateway_user):
    conn = domain_connection
    material_id = add_material(conn, gateway_user)
    other = User.anonymous(now=NOW)
    UserRepository(conn).add(other)
    conn.commit()
    request = GatewayRequest(other.id, identity(materials=[MaterialRevision(material_id, "v1")]))
    with pytest.raises(ApiError) as error:
        result_repository.save_in_transaction(conn, request, {"score": 70})
    assert error.value.status == 404 and error.value.code == "NO_MATERIAL"
    with pytest.raises(InvalidOutput):
        result_repository.save_in_transaction(conn, replace(request, owner_id=gateway_user), {"score": float("nan")})
    assert conn.execute(select(t.ai_saved_results)).first() is None


@pytest.fixture
def saved_demo(api_client, demo_user, domain_connection, end_focus):
    headers = {"X-Anonymous-Id": demo_user}
    active = api_client.get("/api/study/sessions/active", headers=headers).json()["data"]
    end_focus(demo_user)
    recorded = api_client.post("/api/records", headers=headers, json={"taskId": active["taskId"],
        "sessionId": active["id"], "result": "partial", "toPage": 13})
    assert recorded.status_code == 200, recorded.text
    opened = api_client.get(f"/api/quiz/{active['subjectId']}", headers=headers)
    assert opened.status_code == 200, opened.text
    quiz = opened.json()["data"]
    rows = domain_connection.execute(select(t.questions, t.quiz_items.c.id.label("item_id"))
        .join(t.quiz_items, t.quiz_items.c.question_id == t.questions.c.id)
        .where(t.quiz_items.c.quiz_session_id == quiz["quizId"])).mappings().all()
    answers = []
    for question in rows:
        answer = {"questionId": question["item_id"], "confidence": "sure"}
        if question["choices"]:
            answer["choice"] = next(index for index, option in enumerate(question["choices"])
                                    if option["text"] == question["correct_answer"])
        else:
            answer["text"] = DEMO_ESSAY["answer"] if question["type"] == "essay" else question["correct_answer"]
        answers.append(answer)
    essay = dict(next(question for question in rows if question["type"] == "essay"))
    domain_connection.commit()
    return {"user": demo_user, "headers": headers, "quiz": quiz["quizId"], "essay": essay,
            "body": {"quizId": quiz["quizId"], "answers": answers, "savedGrading": True}}


@pytest.fixture
def cached_execution_only(monkeypatch):
    original = ProcessExecutor.execute
    calls = []
    def execute(self, identity, *, provider, cached_json=None, **kwargs):
        assert provider is None, "Saved grading must not initialize a provider or credentials"
        assert cached_json is not None
        calls.append(parse_json(cached_json))
        return original(self, identity, provider=None, cached_json=cached_json, **kwargs)
    monkeypatch.setattr(ProcessExecutor, "execute", execute)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return calls


def submit_saved(client, case):
    return client.post(f"/api/quiz/{case['quiz']}/submit", headers=case["headers"], json=case["body"])


def assert_saved_failure(client, case, conn):
    response = submit_saved(client, case)
    assert response.status_code == 503 and response.json()["error"]["code"] == "AI_FAILED", response.text
    repo = QuizAnswerRepository(conn)
    answers = repo.list_current(case["user"], case["quiz"])
    assert len(answers) == len(case["body"]["answers"])
    for expected in case["body"]["answers"]:
        actual = next(row for row in answers if row["quiz_item_id"] == expected["questionId"])
        assert {key: actual[key] for key in ("choice", "text", "confidence")} == {
            key: expected.get(key) for key in ("choice", "text", "confidence")}
    assert repo.submission(case["user"], case["quiz"])["result_snapshot"] is None
    assert conn.execute(select(t.quiz_attempts).join(t.quiz_items,
        t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id).where(
        t.quiz_items.c.quiz_session_id == case["quiz"])).first() is None
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    conn.commit()
    result = client.get(f"/api/quiz/{case['quiz']}/result", headers=case["headers"])
    assert result.status_code == 503


@pytest.mark.parametrize("saved", [True, False])
@pytest.mark.parametrize("confidence", ["sure", "unsure"])
def test_owned_seed_feedback_matches_real_worker_and_replays_without_ai(
        api_client, saved_demo, domain_connection, cached_execution_only, saved, confidence):
    case, conn = saved_demo, domain_connection
    case["body"]["savedGrading"] = saved
    next(answer for answer in case["body"]["answers"] if answer["questionId"] == case["essay"]["item_id"])["confidence"] = confidence
    before = [dict(row) for row in conn.execute(select(t.ai_saved_results)).mappings()]
    assert len(before) == 2 and {row["user_id"] for row in before} == {case["user"]}
    conn.commit()
    response = submit_saved(api_client, case)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    essay = next(row for row in result["items"] if row["type"] == "essay")
    assert result["essayGrading"] == "saved" and result["summary"]["correct"] == 5
    assert essay["score"] == 70 and essay["confidence"] == confidence
    assert essay["feedback"] == {"good": " ".join(DEMO_ESSAY["good"]), "miss": " ".join(DEMO_ESSAY["missing"])}
    assert essay["evidence"]["page"] == 13
    assert len(cached_execution_only) == 1
    job = conn.execute(select(t.quiz_grading_jobs).join(t.quiz_answers,
        t.quiz_answers.c.id == t.quiz_grading_jobs.c.answer_id).where(
        t.quiz_answers.c.quiz_item_id == case["essay"]["item_id"])).mappings().one()
    assert job["result"]["provenance"]["saved_result_id"] in {row["id"] for row in before}
    assert job["execution_mode"] == ("saved" if saved else "live")
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    assert [dict(row) for row in conn.execute(select(t.ai_saved_results)).mappings()] == before
    conn.commit()
    assert submit_saved(api_client, case).json() == response.json()
    assert api_client.get(f"/api/quiz/{case['quiz']}/result", headers=case["headers"]).json() == response.json()
    assert len(cached_execution_only) == 1


@pytest.mark.parametrize("change", ["answer", "raw_whitespace", "question", "source", "material_hash",
                                    "generation_version", "analysis_version", "MODEL_VERSION", "PROMPT_VERSION", "SCHEMA_VERSION"])
def test_saved_mismatch_keeps_answers_and_never_repairs_fixture_or_calls_ai(
        api_client, saved_demo, domain_connection, cached_execution_only, monkeypatch, change):
    case, conn = saved_demo, domain_connection
    essay = case["essay"]
    before = conn.execute(select(t.ai_saved_results)).all()
    if change in ("answer", "raw_whitespace"):
        answer = next(row for row in case["body"]["answers"] if row["questionId"] == essay["item_id"])
        answer["text"] = "다른 답안" if change == "answer" else DEMO_ESSAY["answer"] + " "
    elif change in ("question", "generation_version"):
        conn.execute(update(t.questions).where(t.questions.c.id == essay["id"]).values(
            {"prompt" if change == "question" else "generation_version": "changed"}))
    elif change == "source":
        conn.execute(update(t.question_sources).where(t.question_sources.c.question_id == essay["id"]).values(quote="변경된 근거"))
    elif change in ("material_hash", "analysis_version"):
        material_id = conn.scalar(select(t.units.c.material_id).where(t.units.c.id == essay["unit_id"]))
        conn.execute(update(t.materials).where(t.materials.c.id == material_id).values(
            {"sha256" if change == "material_hash" else "analysis_version": "b" * 64}))
    else:
        monkeypatch.setattr(grading, change, "changed-version")
    if change not in ("answer", "raw_whitespace"):
        assert register_saved_evaluation(conn, case["user"], essay["id"]) == []
    conn.commit()
    assert_saved_failure(api_client, case, conn)
    assert cached_execution_only == []
    assert conn.execute(select(t.ai_saved_results)).all() == before


@pytest.mark.parametrize("cache", ["foreign", "missing", "invalid_result"])
def test_saved_cache_is_owned_and_revalidated_without_live_fallback(
        api_client, saved_demo, domain_connection, cached_execution_only, cache):
    case, conn = saved_demo, domain_connection
    if cache == "foreign":
        other = User.anonymous(now=NOW)
        UserRepository(conn).add(other)
        # Even an exact input hash under another owner cannot be reused.
        conn.execute(update(t.ai_saved_results).values(user_id=other.id))
    elif cache == "missing":
        conn.execute(delete(t.ai_saved_results))
    else:
        conn.execute(update(t.ai_saved_results).values(result={"score": 101, "strengths": [],
            "missing_points": [], "feedback": "invalid"}))
    conn.commit()
    assert_saved_failure(api_client, case, conn)
    assert len(cached_execution_only) == (1 if cache == "invalid_result" else 0)


def test_saved_mode_survives_budget_exhaustion_and_reclaimed_worker(
        api_client, saved_demo, domain_connection, cached_execution_only, monkeypatch):
    from app.db.session import engine
    case, conn = saved_demo, domain_connection
    with monkeypatch.context() as exhausted:
        exhausted.setattr(grading.GradingBudget, "remaining", property(lambda _: 0))
        assert_saved_failure(api_client, case, conn)
    jobs = [dict(row) for row in conn.execute(select(t.quiz_grading_jobs, t.quiz_answers.c.quiz_item_id)
        .join(t.quiz_answers, t.quiz_answers.c.id == t.quiz_grading_jobs.c.answer_id)).mappings()]
    assert {job["execution_mode"] for job in jobs} == {"saved"}
    job = next(job for job in jobs if job["quiz_item_id"] == case["essay"]["item_id"])
    conn.commit()
    first = grading.GradingService(engine, owner="before-restart")
    with engine.begin() as transaction:
        prepared = first.prepare(transaction, case["user"], job["id"])
    assert isinstance(prepared, grading.PreparedGrading) and prepared.snapshot.mode == "saved"
    restarted = grading.GradingService(engine, owner="after-restart", clock=lambda: now_utc() + timedelta(minutes=1))
    completed = restarted.run_job(case["user"], job["id"])
    assert completed.state == "succeeded" and completed.result["essay_score"] == 70
    assert first.execute(prepared).state == "stale"
    case["body"]["savedGrading"] = False
    response = submit_saved(api_client, case)
    assert response.status_code == 200 and response.json()["data"]["essayGrading"] == "saved", response.text
    assert set(conn.scalars(select(t.quiz_grading_jobs.c.execution_mode))) == {"saved"}
    assert conn.scalar(select(t.quiz_grading_jobs.c.claim_count).where(t.quiz_grading_jobs.c.id == job["id"])) == 2
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    assert len(cached_execution_only) == 1
