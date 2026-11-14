from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository
from app.services import quiz_answers as service

NOW = datetime(2026, 11, 14, 14, 59, 59, tzinfo=timezone.utc)
DAY = date(2026, 11, 14)
ANSWER_TABLES = (t.quiz_answers, t.quiz_answer_heads, t.quiz_answer_receipts, t.quiz_grading_jobs,
                 t.quiz_submission_states, t.quiz_attempts, t.mastery_history, t.weak_questions)


def make_case(conn):
    ids = {name: str(uuid4()) for name in ("user", "subject", "material", "unit", "quiz")}
    conn.execute(t.users.insert().values(id=ids["user"], demo_date=DAY, created_at=NOW, updated_at=NOW))
    conn.execute(t.subjects.insert().values(id=ids["subject"], user_id=ids["user"], code="DB", name="자료",
                                          color="#123456", status="active", created_at=NOW, updated_at=NOW))
    conn.execute(t.materials.insert().values(id=ids["material"], subject_id=ids["subject"],
        original_name="book.pdf", source_format="pdf", sha256="a" * 64, analysis_version="1",
        quiz_enabled=True, page_count=10, created_at=NOW, updated_at=NOW))
    conn.execute(t.units.insert().values(id=ids["unit"], material_id=ids["material"], code="DB1", title="단원",
        sort_order=1, start_page=1, end_page=10, created_at=NOW, updated_at=NOW))
    conn.execute(t.quiz_sessions.insert().values(id=ids["quiz"], user_id=ids["user"], subject_id=ids["subject"],
        type="practice", quiz_date=DAY, status="ready", created_at=NOW))
    for index, kind in enumerate(("choice", "ox", "short", "essay"), 1):
        ids[kind], ids[f"{kind}_item"] = str(uuid4()), str(uuid4())
        conn.execute(t.questions.insert().values(id=ids[kind], unit_id=ids["unit"], code=kind, type=kind,
            difficulty="low", prompt=kind, choices=[{"text": "맞음"}, {"text": "틀림"}]
            if kind in ("choice", "ox") else None, correct_answer="맞음", explanation="비공개 해설", created_at=NOW))
        conn.execute(t.question_sources.insert().values(id=str(uuid4()), question_id=ids[kind],
            material_id=ids["material"], start_page=1, end_page=1, quote="비공개 근거"))
        conn.execute(t.quiz_items.insert().values(id=ids[f"{kind}_item"], quiz_session_id=ids["quiz"],
            question_id=ids[kind], sort_order=index, inclusion_reason="practice"))
    conn.commit()
    return ids


@pytest.fixture
def answer_case(domain_connection):
    return make_case(domain_connection)


def put(client, case, kind="essay", **changes):
    payload = {"choice": 0, "text": None} if kind in ("choice", "ox") else {"choice": None, "text": "  원문 답안\n"}
    return client.put(f"/api/quiz/{case['quiz']}/answers/{case[f'{kind}_item']}",
        headers={"X-Anonymous-Id": case["user"]},
        json={**payload, "confidence": "sure", "revision": 0, **changes})


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"


def snapshot(conn):
    return {table.name: conn.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in (*ANSWER_TABLES, t.quiz_sessions, t.units)}


@pytest.mark.parametrize("kind", ["choice", "ox", "short", "essay"])
def test_save_commits_answer_and_job_without_grading(api_client, answer_case, domain_connection, monkeypatch, kind):
    case = answer_case
    no_ai = Mock(side_effect=AssertionError("answer saving must not call AI"))
    monkeypatch.setattr("app.ai.engine.module", no_ai)
    monkeypatch.setattr(service, "now_utc", lambda: NOW)
    response = put(api_client, case, kind)
    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "data": {
        "quizId": case["quiz"], "questionId": case[f"{kind}_item"], "saved": True, "revision": 1}}
    assert response.headers["cache-control"] == "no-store"
    # Request dependency has committed; an unrelated connection can see both.
    from app.db.session import engine
    with engine.connect() as restarted:
        answer = QuizAnswerRepository(restarted).current(case["user"], case["quiz"], case[f"{kind}_item"])
        job = restarted.execute(select(t.quiz_grading_jobs)).mappings().one()
        assert job["answer_id"] == answer["id"] and job["answer_revision"] == 1
        assert job["status"] == "queued" and job["result"] is None
        assert answer["answered_at"] == NOW and answer["answered_date"] == DAY
        assert answer["choice"] == (0 if kind in ("choice", "ox") else None)
        assert answer["text"] == (None if kind in ("choice", "ox") else "  원문 답안\n")
        for table in (t.quiz_attempts, t.mastery_history, t.weak_questions, t.quiz_submission_states):
            assert restarted.execute(select(table)).first() is None
    no_ai.assert_not_called()


@pytest.mark.parametrize("kind,value", [("short", ""), ("essay", ""), ("essay", " \n\t"), ("essay", "한글😀" * 2000)])
def test_explicit_empty_text_and_unbounded_original_text_are_saved(api_client, answer_case, domain_connection, kind, value):
    assert put(api_client, answer_case, kind, text=value, confidence="unsure").status_code == 200
    answer = domain_connection.execute(select(t.quiz_answers)).mappings().one()
    assert answer["text"] == value and answer["confidence"] == "unsure"


@pytest.mark.parametrize("kind,change", [
    ("choice", {"choice": value}) for value in (-1, 2, 100000, 0.0, "0", True, False, None, [], {})
] + [
    ("essay", {"text": value}) for value in (None, 1, True, [], {})
] + [
    ("essay", {"revision": value}) for value in (-1, True, False, 0.0, "0", None, [], {})
] + [
    ("essay", {"confidence": value}) for value in (None, "", "SURE", "certain", True, 1, [], {})
] + [("choice", {"text": ""}), ("essay", {"choice": 0}), ("essay", {"gradingStatus": "done"}),
     ("essay", {"savedGrading": True})])
def test_strict_invalid_payload_never_writes(api_client, answer_case, domain_connection, kind, change):
    before = snapshot(domain_connection)
    domain_connection.commit()
    error(put(api_client, answer_case, kind, **change), 422, "INVALID_INPUT")
    assert snapshot(domain_connection) == before


@pytest.mark.parametrize("omit,status,code", [("confidence", 400, "CONFIDENCE_REQUIRED"),
    ("revision", 422, "INVALID_INPUT"), ("text", 422, "INVALID_INPUT")])
def test_required_fields(api_client, answer_case, omit, status, code):
    case = answer_case
    body = {"text": "답", "confidence": "sure", "revision": 0}
    del body[omit]
    error(api_client.put(f"/api/quiz/{case['quiz']}/answers/{case['essay_item']}",
        headers={"X-Anonymous-Id": case["user"]}, json=body), status, code)


@pytest.mark.parametrize("location", ["quiz", "essay_item"])
def test_path_uuids_are_validated_and_normalized(api_client, answer_case, location):
    error(put(api_client, {**answer_case, location: "bad-id"}), 422, "INVALID_INPUT")
    normalized = {**answer_case, location: answer_case[location].replace("-", "").upper()}
    response = put(api_client, normalized)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["quizId"] == answer_case["quiz"]
    assert response.json()["data"]["questionId"] == answer_case["essay_item"]


@pytest.mark.parametrize("variant,status,code", [
    ("foreign_user", 404, "NO_QUIZ"), ("foreign_quiz", 404, "NO_QUIZ"), ("unknown_quiz", 404, "NO_QUIZ"),
    ("foreign_item", 422, "INVALID_INPUT"), ("unknown_item", 422, "INVALID_INPUT"),
    ("original_question", 422, "INVALID_INPUT"), ("other_set_same_question", 422, "INVALID_INPUT"),
    ("foreign_material", 404, "NO_QUIZ"), ("foreign_parent", 404, "NO_QUIZ"),
])
def test_set_item_and_parent_ownership(api_client, answer_case, domain_connection, variant, status, code):
    conn, case = domain_connection, dict(answer_case)
    other = make_case(conn)
    if variant == "foreign_user":
        case["user"] = other["user"]
    elif variant == "foreign_quiz":
        case["quiz"] = other["quiz"]
    elif variant == "unknown_quiz":
        case["quiz"] = str(uuid4())
    elif variant == "foreign_item":
        case["essay_item"] = other["essay_item"]
    elif variant == "unknown_item":
        case["essay_item"] = str(uuid4())
    elif variant == "original_question":
        case["essay_item"] = case["essay"]
    elif variant == "other_set_same_question":
        conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == other["essay_item"]).values(question_id=case["essay"]))
        case["essay_item"] = other["essay_item"]
    elif variant == "foreign_material":
        conn.execute(update(t.units).where(t.units.c.id == case["unit"]).values(material_id=other["material"]))
    else:
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(subject_id=other["subject"]))
    before = snapshot(conn)
    conn.commit()
    error(put(api_client, case), status, code)
    assert snapshot(conn) == before


def test_receipts_noops_edits_and_stale_revision_conflicts(api_client, answer_case, domain_connection, monkeypatch):
    case, conn = answer_case, domain_connection
    first = put(api_client, case).json()
    first_answer = QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"])
    conn.commit()
    # Both an exact retry and an acknowledged no-op keep the original timestamp.
    for revision in (0, 1, 1):
        assert put(api_client, case, revision=revision).json() == first
    assert put(api_client, case, revision=1, text="수정").json()["data"]["revision"] == 2
    before = snapshot(conn)
    conn.commit()
    monkeypatch.setattr(GradingJobRepository, "enqueue", Mock(side_effect=AssertionError("replay must not enqueue")))
    for revision in (0, 1):
        assert put(api_client, case, revision=revision).json() == first
    for changes in ({"revision": 0, "text": "수정"}, {"revision": 0, "confidence": "unsure"},
                    {"revision": 3}, {"revision": 10 ** 100}):
        error(put(api_client, case, **changes), 409, "ANSWER_CONFLICT")
    assert snapshot(conn) == before
    current = QuizAnswerRepository(conn).current(case["user"], case["quiz"], case["essay_item"])
    assert current["revision"] == 2 and current["text"] == "수정"
    original = conn.execute(select(t.quiz_answers).where(t.quiz_answers.c.id == first_answer["id"])).mappings().one()
    assert original["text"] == first_answer["text"]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("after_insert", [False, True])
def test_enqueue_failure_rolls_back_answer_head_receipt_and_old_job(api_client, answer_case, domain_connection,
                                                                   monkeypatch, existing, after_insert):
    case, conn = answer_case, domain_connection
    if existing:
        assert put(api_client, case).status_code == 200
    before = snapshot(conn)
    conn.commit()
    enqueue = GradingJobRepository.enqueue
    def fail(self, *args, **kwargs):
        if after_insert:
            enqueue(self, *args, **kwargs)
        raise RuntimeError("injected job enqueue failure")
    monkeypatch.setattr(GradingJobRepository, "enqueue", fail)
    error(put(api_client, case, revision=int(existing), text="변경"), 500, "SERVER_ERROR")
    assert snapshot(conn) == before


@pytest.mark.parametrize("state", ["submitted", "carried_over", "finalized", "sealed"])
def test_finalization_preserves_received_replays_but_rejects_new_requests(api_client, answer_case, domain_connection, state):
    case, conn = answer_case, domain_connection
    first = put(api_client, case).json()
    if state == "sealed":
        for kind in ("choice", "ox", "short"):
            assert put(api_client, case, kind).status_code == 200
        QuizAnswerRepository(conn).seal(case["user"], case["quiz"],
            expected_revisions={case[f"{kind}_item"]: 1 for kind in ("choice", "ox", "short", "essay")}, now=NOW)
    else:
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(
            **({"finalized_at": NOW} if state == "finalized" else {"status": state})))
        assert QuizAnswerRepository(conn).submission(case["user"], case["quiz"]) is None
    before = snapshot(conn)
    conn.commit()
    assert put(api_client, case).json() == first
    # Same input at an unseen request revision is not an old receipt.
    error(put(api_client, case, revision=1), 409, "QUIZ_FINALIZED")
    error(put(api_client, case, revision=1, text="다른 답"), 409, "QUIZ_FINALIZED")
    assert snapshot(conn) == before


@pytest.mark.parametrize("state", ["submitted", "carried_over", "finalized", "generating", "failed"])
def test_closed_or_unready_legacy_set_never_gets_new_answers(api_client, answer_case, domain_connection, state):
    case, conn = answer_case, domain_connection
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(
        **({"finalized_at": NOW} if state == "finalized" else {"status": state})))
    before = snapshot(conn)
    conn.commit()
    if state in ("generating", "failed"):
        error(put(api_client, case), 404, "NO_QUIZ")
    else:
        error(put(api_client, case), 409, "QUIZ_FINALIZED")
    assert snapshot(conn) == before


def make_daily(conn, case):
    plan, block, task, record, record_range = (str(uuid4()) for _ in range(5))
    conn.execute(t.plans.insert().values(id=plan, user_id=case["user"], created_at=NOW, approved_at=NOW))
    conn.execute(t.study_blocks.insert().values(id=block, subject_id=case["subject"], unit_id=case["unit"],
        task_type="study", created_at=NOW))
    conn.execute(t.study_tasks.insert().values(id=task, plan_id=plan, subject_id=case["subject"], block_id=block,
        task_type="study", title="학습", scheduled_date=DAY, estimated_minutes=10, status="partial", created_at=NOW))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=task, unit_id=case["unit"],
        start_page=1, end_page=10, sort_order=0))
    conn.execute(t.study_records.insert().values(id=record, task_id=task, result="partial", recorded_at=NOW,
        study_date=DAY, updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=record_range, record_id=record, unit_id=case["unit"],
        start_page=1, end_page=7))
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == case["quiz"]).values(type="daily"))
    conn.commit()
    return task, record_range


@pytest.mark.parametrize("change", ["progress", "quizless", "scope", "excluded", "new_day"])
def test_current_eligibility_is_rechecked_except_received_receipts(api_client, answer_case, domain_connection, change):
    case, conn = answer_case, domain_connection
    task, record_range = make_daily(conn, case)
    first = put(api_client, case).json()
    assert first["success"], first
    if change == "progress":
        conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.id == record_range).values(end_page=6))
    elif change == "quizless":
        conn.execute(update(t.materials).where(t.materials.c.id == case["material"]).values(quiz_enabled=False))
    elif change == "scope":
        conn.execute(update(t.units).where(t.units.c.id == case["unit"]).values(in_scope=False))
    elif change == "excluded":
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task).values(status="excluded"))
    else:
        conn.execute(update(t.users).where(t.users.c.id == case["user"]).values(demo_date=DAY + timedelta(days=1)))
    before = snapshot(conn)
    conn.commit()
    assert put(api_client, case).json() == first
    error(put(api_client, case, revision=1), 403, "QUIZ_LOCKED")
    error(put(api_client, case, revision=1, text="수정"), 403, "QUIZ_LOCKED")
    assert snapshot(conn) == before


@pytest.mark.parametrize("demo", [False, True])
def test_answer_date_uses_fresh_locked_user_and_one_instant(api_client, answer_case, domain_connection, monkeypatch, demo):
    case = answer_case
    original = service.lock_user
    def lock_then_refresh(conn, user_id):
        original(conn, user_id)
        conn.execute(update(t.users).where(t.users.c.id == str(user_id)).values(
            demo_date=DAY + timedelta(days=7) if demo else None, timezone="Asia/Seoul"))
        return original(conn, user_id)
    clock = Mock(return_value=NOW + timedelta(seconds=1))
    monkeypatch.setattr(service, "lock_user", lock_then_refresh)
    monkeypatch.setattr(service, "now_utc", clock)
    assert put(api_client, case).status_code == 200
    answer = QuizAnswerRepository(domain_connection).current(case["user"], case["quiz"], case["essay_item"])
    assert answer["answered_date"] == DAY + timedelta(days=7 if demo else 1)
    assert answer["answered_at"] == NOW + timedelta(seconds=1)
    clock.assert_called_once_with()
