from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import event, select

from app.core.responses import ApiError
from app.db import schema as t, session
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services import quiz_answers as service
from tests.test_quiz_answer_api import NOW, answer_case, error, put


@pytest.fixture
def fast_lock_failure(api_client, domain_connection):
    if domain_connection.dialect.name == "sqlite":
        @event.listens_for(session.engine, "checkout")
        def limit_wait(connection, *_):
            connection.execute("PRAGMA busy_timeout=20")


@pytest.mark.parametrize("same_input", [False, True])
def test_revision_competitors_serialize_and_retry_without_extra_job(api_client, answer_case, domain_connection,
                                                                  fast_lock_failure, monkeypatch, same_input):
    case = answer_case
    entered, release = Event(), Event()
    real = GradingJobRepository.enqueue
    def held(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(GradingJobRepository, "enqueue", held)
    changes = {} if same_input else {"text": "다른 창의 답안"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(put, api_client, case)
        try:
            assert entered.wait(5)
            error(put(api_client, case, **changes), 409, "REQUEST_IN_PROGRESS")
        finally:
            release.set()
        saved = first.result(timeout=5)
    assert saved.status_code == 200, saved.text
    second = put(api_client, case, **changes)
    if same_input:
        assert second.json() == saved.json()
    else:
        error(second, 409, "ANSWER_CONFLICT")
    assert len(domain_connection.execute(select(t.quiz_answers)).all()) == 1
    assert len(domain_connection.execute(select(t.quiz_grading_jobs)).all()) == 1
    assert len(domain_connection.execute(select(t.quiz_answer_receipts)).all()) == 1


def save_all(client, case):
    expected = {}
    for kind in ("choice", "ox", "short", "essay"):
        result = put(client, case, kind)
        assert result.status_code == 200, result.text
        expected[case[f"{kind}_item"]] = result.json()["data"]["revision"]
    return expected


def test_seal_first_blocks_edit_then_only_received_retry_succeeds(api_client, answer_case, domain_connection,
                                                                fast_lock_failure):
    case, conn = answer_case, domain_connection
    expected = save_all(api_client, case)
    QuizAnswerRepository(conn).seal(case["user"], case["quiz"], expected_revisions=expected, now=NOW)
    try:
        error(put(api_client, case, revision=1, text="봉인 중 수정"), 409, "REQUEST_IN_PROGRESS")
    finally:
        conn.commit()
    error(put(api_client, case, revision=1, text="봉인 후 수정"), 409, "QUIZ_FINALIZED")
    assert put(api_client, case).json()["data"]["revision"] == 1
    assert len(conn.execute(select(t.quiz_answers)).all()) == 4
    assert len(conn.execute(select(t.quiz_grading_jobs)).all()) == 4


def test_edit_first_requires_sealer_to_refresh_expected_revisions(api_client, answer_case, domain_connection,
                                                                fast_lock_failure, monkeypatch):
    case = answer_case
    expected = save_all(api_client, case)
    entered, release = Event(), Event()
    real = GradingJobRepository.enqueue
    def held(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(GradingJobRepository, "enqueue", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        editing = pool.submit(put, api_client, case, revision=1, text="수정된 답안")
        try:
            assert entered.wait(5)
            with pytest.raises(ApiError) as busy, session.engine.begin() as other:
                QuizAnswerRepository(other).seal(case["user"], case["quiz"], expected_revisions=expected, now=NOW)
            assert busy.value.status == 409 and busy.value.code == "REQUEST_IN_PROGRESS"
        finally:
            release.set()
        assert editing.result(timeout=5).status_code == 200
    with pytest.raises(ApiError) as stale, session.engine.begin() as other:
        QuizAnswerRepository(other).seal(case["user"], case["quiz"], expected_revisions=expected, now=NOW)
    assert stale.value.status == 409 and stale.value.code == "ANSWER_CONFLICT"
    expected[case["essay_item"]] = 2
    with session.engine.begin() as other:
        sealed = QuizAnswerRepository(other).seal(case["user"], case["quiz"], expected_revisions=expected, now=NOW)
    assert {a["questionId"]: a["revision"] for a in sealed["sealed_answers"]} == expected
    error(put(api_client, case, revision=2, text="재수정"), 409, "QUIZ_FINALIZED")


def test_http_edit_rejects_late_worker_result_and_retains_ungraded_input(api_client, answer_case, domain_connection, monkeypatch):
    case, conn = answer_case, domain_connection
    monkeypatch.setattr(service, "now_utc", lambda: NOW)
    assert put(api_client, case).status_code == 200
    jobs = GradingJobRepository(conn)
    first = conn.execute(select(t.quiz_grading_jobs)).mappings().one()
    lease = jobs.claim(case["user"], first["id"], owner="worker-a", now=NOW, lease_for=timedelta(minutes=1))
    conn.commit()
    assert put(api_client, case, revision=1, text="새 답안").status_code == 200
    assert not jobs.complete(case["user"], lease["id"], owner="worker-a", token=lease["lease_token"],
        revision=1, now=NOW + timedelta(seconds=1), result={"is_correct": True, "essay_score": 100})
    assert jobs.get(case["user"], lease["id"])["status"] == "cancelled"
    answer = QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"])
    assert answer["revision"] == 2 and answer["text"] == "새 답안"
    current_job = conn.execute(select(t.quiz_grading_jobs).where(t.quiz_grading_jobs.c.answer_id == answer["id"])).mappings().one()
    assert current_job["status"] == "queued" and current_job["result"] is None
    assert conn.execute(select(t.quiz_attempts)).first() is None
    assert conn.execute(select(t.mastery_history)).first() is None
