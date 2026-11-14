from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.services import quiz_answers as service
from tests.test_quiz_answer_api import NOW, DAY, answer_case, error, make_case, make_daily, put, snapshot
from tests.test_quiz_identifiers import quiz_case


def get(client, case, **params):
    return client.get(f"/api/quiz/{case['subject']}", headers={"X-Anonymous-Id": case["user"]},
                      params={"quizId": case["quiz"], **params})


def test_unanswered_get_is_empty_and_read_only(api_client, answer_case, domain_connection):
    before = snapshot(domain_connection)
    domain_connection.commit()
    response = get(api_client, answer_case)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["quizId"] == answer_case["quiz"]
    assert response.json()["data"]["savedAnswers"] == []
    assert snapshot(domain_connection) == before


def test_reopened_get_recovers_only_current_inputs_in_question_order(api_client, answer_case, domain_connection, monkeypatch):
    case, conn = answer_case, domain_connection
    monkeypatch.setattr(service, "now_utc", lambda: NOW)
    # Different save order must not change the question order on recovery.
    for kind in ("essay", "short", "ox", "choice"):
        assert put(api_client, case, kind).status_code == 200
    assert put(api_client, case, revision=1, text="저장한 최신 원문", confidence="unsure").status_code == 200
    assert put(api_client, case).json()["data"]["revision"] == 1  # old receipt does not rewind GET
    job_id = conn.scalar(select(t.quiz_grading_jobs.c.id).join(t.quiz_answers,
        t.quiz_answers.c.id == t.quiz_grading_jobs.c.answer_id).where(
            t.quiz_answers.c.quiz_item_id == case["essay_item"], t.quiz_answers.c.revision == 2))
    jobs = GradingJobRepository(conn)
    lease = jobs.claim(case["user"], job_id, owner="worker", now=NOW, lease_for=timedelta(minutes=1))
    assert jobs.complete(case["user"], job_id, owner="worker", token=lease["lease_token"], revision=2,
        now=NOW + timedelta(seconds=1), result={"is_correct": False, "essay_score": 17,
                                               "feedback": "비공개 피드백", "grading_status": "graded"})
    before = snapshot(conn)
    conn.commit()
    response = get(api_client, case, saved=1)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["savedAnswers"] == [
        {"questionId": case["choice_item"], "choice": 0, "text": None, "confidence": "sure", "revision": 1},
        {"questionId": case["ox_item"], "choice": 0, "text": None, "confidence": "sure", "revision": 1},
        {"questionId": case["short_item"], "choice": None, "text": "  원문 답안\n", "confidence": "sure", "revision": 1},
        {"questionId": case["essay_item"], "choice": None, "text": "저장한 최신 원문", "confidence": "unsure", "revision": 2},
    ]
    forbidden = {"answer", "answerId", "answerHash", "is_correct", "correct", "score", "essay_score",
                 "explanation", "evidence", "page", "quote", "feedback", "gradingStatus", "grading_status",
                 "result", "lease_token", "legacy_answer", "input_recovered"}
    def check(value):
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(data)
    assert "비공개" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert snapshot(conn) == before
    direct = api_client.get(f"/api/quiz/{case['quiz']}", headers={"X-Anonymous-Id": case["user"]})
    assert direct.json()["data"]["savedAnswers"] == data["savedAnswers"]


def test_legacy_unrecoverable_input_is_not_guessed_or_rewritten(api_client, answer_case, domain_connection):
    case, conn = answer_case, domain_connection
    assert put(api_client, case, "short", text="복원된 원문").status_code == 200
    legacy_id = str(uuid4())
    conn.execute(t.quiz_answers.insert().values(id=legacy_id, quiz_item_id=case["choice_item"], revision=1,
        input_recovered=False, legacy_answer="보기 변경 전 비공개 원문", confidence="sure", answered_at=NOW, answered_date=DAY))
    conn.execute(t.quiz_answer_heads.insert().values(quiz_item_id=case["choice_item"], answer_id=legacy_id))
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(status="submitted", finalized_at=NOW))
    before = snapshot(conn)
    conn.commit()
    response = get(api_client, case)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["savedAnswers"] == [{"questionId": case["short_item"], "choice": None,
        "text": "복원된 원문", "confidence": "sure", "revision": 1}]
    assert "보기 변경 전" not in response.text
    error(put(api_client, case, "choice", revision=1), 409, "QUIZ_FINALIZED")
    assert snapshot(conn) == before


def test_legacy_submission_without_sidecar_is_closed_and_not_reconstructed(api_client, quiz_case, domain_connection):
    case = quiz_case
    # Seed the historical representation directly. The current POST now owns
    # durable answer revisions and must not be used to manufacture legacy data.
    domain_connection.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case['quiz_id']).values(
        status='submitted', finalized_at=NOW))
    for item in case['items']:
        domain_connection.execute(t.quiz_attempts.insert().values(id=str(uuid4()), quiz_item_id=item['id'],
            answer='legacy raw answer', confidence='sure', is_correct=False, grading_status='graded',
            answered_at=NOW, answered_date=DAY, finalized_at=NOW))
    domain_connection.commit()
    assert domain_connection.scalar(select(t.quiz_submission_states.c.quiz_session_id).where(
        t.quiz_submission_states.c.quiz_session_id == case["quiz_id"])) is None
    before = snapshot(domain_connection)
    domain_connection.commit()
    viewed = api_client.get(f"/api/quiz/{case['quiz_id']}", headers=case["headers"])
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["data"]["savedAnswers"] == []
    changed = api_client.put(f"/api/quiz/{case['quiz_id']}/answers/{case['items'][0]['id']}", headers=case["headers"],
        json={"choice": 1, "text": None, "confidence": "sure", "revision": 0})
    error(changed, 409, "QUIZ_FINALIZED")
    assert snapshot(domain_connection) == before


@pytest.mark.parametrize("corrupt", ["owner", "parent", "material", "item"])
def test_recovery_rejects_foreign_set_and_material_even_without_answers(api_client, answer_case, domain_connection, corrupt):
    case, conn = dict(answer_case), domain_connection
    other = make_case(conn)
    if corrupt == "owner":
        case["user"] = other["user"]
    elif corrupt == "parent":
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(subject_id=other["subject"]))
    elif corrupt == "material":
        conn.execute(update(t.units).where(t.units.c.id == case["unit"]).values(
            material_id=other["material"], code="FOREIGN2", sort_order=2))
    else:
        conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == case["essay_item"]).values(question_id=other["essay"]))
    before = snapshot(conn)
    conn.commit()
    error(get(api_client, case), 404, "NO_QUIZ")
    assert snapshot(conn) == before


def test_same_source_question_in_different_set_does_not_recover_other_answer(api_client, answer_case, domain_connection):
    case, conn = answer_case, domain_connection
    assert put(api_client, case).status_code == 200
    second_quiz, second_item = str(uuid4()), str(uuid4())
    conn.execute(t.quiz_sessions.insert().values(id=second_quiz, user_id=case["user"], subject_id=case["subject"],
        type="practice", quiz_date=DAY, status="ready", created_at=NOW))
    conn.execute(t.quiz_items.insert().values(id=second_item, quiz_session_id=second_quiz, question_id=case["essay"],
        sort_order=1, inclusion_reason="practice"))
    conn.commit()
    other = get(api_client, {**case, "quiz": second_quiz})
    assert other.status_code == 200, other.text
    assert other.json()["data"]["questions"][0]["id"] == second_item
    assert other.json()["data"]["savedAnswers"] == []
    assert get(api_client, case).json()["data"]["savedAnswers"][0]["questionId"] == case["essay_item"]


def test_recovery_keeps_current_daily_open_rule(api_client, answer_case, domain_connection):
    case, conn = answer_case, domain_connection
    _, record_range = make_daily(conn, case)
    assert put(api_client, case).status_code == 200
    assert get(api_client, case).json()["data"]["savedAnswers"]
    conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.id == record_range).values(end_page=6))
    before = snapshot(conn)
    conn.commit()
    error(get(api_client, case), 403, "QUIZ_LOCKED")
    assert snapshot(conn) == before
