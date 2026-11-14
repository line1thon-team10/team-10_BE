from datetime import datetime, timezone
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.db.migrations import migration_config
from app.seed.loader import load_seed


def seed_legacy(conn):
    user_id = str(uuid4())
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    conn.execute(t.users.insert().values(id=user_id, created_at=now, updated_at=now))
    load_seed(conn, user_id, "day9")
    conn.commit()
    return user_id


def rows(conn, table):
    return [dict(row) for row in conn.execute(select(table).order_by(*table.primary_key.columns)).mappings()]


def test_legacy_answers_and_completed_sets_survive_upgrade_roundtrip(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0005_onboarding_state")
    seed_legacy(conn)
    before = {table.name: rows(conn, table) for table in (t.quiz_attempts, t.quiz_sessions, t.mastery_history)}
    conn.commit()
    for _ in range(2):
        command.upgrade(config, "head")
        for table in (t.quiz_attempts, t.quiz_sessions, t.mastery_history):
            assert rows(conn, table) == before[table.name]
        answers = rows(conn, t.quiz_answers)
        assert len(answers) == len(before["quiz_attempts"]) == len(rows(conn, t.quiz_answer_heads))
        old_by_item = {row["quiz_item_id"]: row for row in before["quiz_attempts"]}
        for answer in answers:
            old = old_by_item[answer["quiz_item_id"]]
            assert answer["revision"] == 1 and answer["input_recovered"]
            assert answer["legacy_answer"] == old["answer"]
            assert answer["answered_at"] == old["answered_at"] and answer["answered_date"] == old["answered_date"]
            assert len(answer["answer_hash"]) == 64 and answer["confidence"] == old["confidence"]
        for state in rows(conn, t.quiz_submission_states):
            assert state["legacy"] and state["input_recovered"] and len(state["answer_set_hash"]) == 64
            assert state["legacy_snapshot"]["session"]["id"] == state["quiz_session_id"]
            assert state["legacy_snapshot"]["attempts"]
            assert state["result_snapshot"] is state["result_provenance"] is state["result_captured_at"] is None
        assert all(row["status"] == "succeeded" and row["claim_count"] == 0 for row in rows(conn, t.quiz_grading_jobs))
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_server_default": True}), t.metadata) == []
        conn.commit()
        command.downgrade(config, "0005_onboarding_state")
        assert "quiz_answers" not in inspect(conn).get_table_names()
        for table in (t.quiz_attempts, t.quiz_sessions, t.mastery_history):
            assert rows(conn, table) == before[table.name]
        conn.commit()


@pytest.mark.parametrize("kind", ["duplicate_choice", "missing_choice"])
def test_ambiguous_choice_is_preserved_without_invented_index_or_hash(migration_connection, kind):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0005_onboarding_state")
    seed_legacy(conn)
    row = conn.execute(select(t.questions.c.id, t.questions.c.choices, t.quiz_attempts.c.answer,
                              t.quiz_items.c.id.label("item_id"), t.quiz_items.c.quiz_session_id)
        .join(t.quiz_items, t.quiz_items.c.question_id == t.questions.c.id)
        .join(t.quiz_attempts, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
        .where(t.questions.c.type.in_(("choice", "ox")))).mappings().first()
    choices = [{"text": row["answer"]}, {"text": row["answer"]}] if kind == "duplicate_choice" else [{"text": "changed"}]
    conn.execute(update(t.questions).where(t.questions.c.id == row["id"]).values(choices=choices))
    conn.commit()
    command.upgrade(config, "head")
    answer = conn.execute(select(t.quiz_answers).where(t.quiz_answers.c.quiz_item_id == row["item_id"])).mappings().one()
    assert answer["legacy_answer"] == row["answer"]
    assert answer["choice"] is answer["text"] is answer["answer_hash"] is None
    assert answer["input_recovered"] is False
    state = conn.execute(select(t.quiz_submission_states).where(
        t.quiz_submission_states.c.quiz_session_id == row["quiz_session_id"])).mappings().one()
    assert state["legacy"] and not state["input_recovered"] and state["answer_set_hash"] is None
    assert state["result_snapshot"] is None and state["legacy_snapshot"]["attempts"]


@pytest.mark.parametrize("field,value", [("confidence", "unknown"), ("essay_score", -1), ("essay_score", 101)])
def test_invalid_legacy_values_stop_before_any_new_ddl(migration_connection, field, value):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0005_onboarding_state")
    seed_legacy(conn)
    attempt_id = conn.scalar(select(t.quiz_attempts.c.id))
    conn.execute(update(t.quiz_attempts).where(t.quiz_attempts.c.id == attempt_id).values(**{field: value}))
    conn.commit()
    with pytest.raises(RuntimeError, match="기존 데이터를 보존"):
        command.upgrade(config, "head")
    conn.rollback()
    assert "quiz_answers" not in inspect(conn).get_table_names()
    assert conn.scalar(select(t.quiz_attempts.c[field]).where(t.quiz_attempts.c.id == attempt_id)) == value


def test_demo_reset_cascades_only_its_quiz_storage(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0005_onboarding_state")
    first, second = seed_legacy(conn), seed_legacy(conn)
    second_quizzes = set(conn.scalars(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id == second)))
    second_items = set(conn.scalars(select(t.quiz_items.c.id).where(t.quiz_items.c.quiz_session_id.in_(second_quizzes))))
    second_questions = set(conn.scalars(select(t.quiz_items.c.question_id).where(t.quiz_items.c.id.in_(second_items))))
    sources = conn.execute(select(t.question_sources).where(t.question_sources.c.question_id.in_(second_questions))).all()
    conn.commit()
    command.upgrade(config, "head")
    expected_answers = [row for row in rows(conn, t.quiz_answers) if row["quiz_item_id"] in second_items]
    conn.commit()
    load_seed(conn, first, "empty")
    conn.commit()
    assert rows(conn, t.quiz_answers) == expected_answers
    assert set(conn.scalars(select(t.quiz_submission_states.c.quiz_session_id))) <= second_quizzes
    assert conn.execute(select(t.question_sources).where(t.question_sources.c.question_id.in_(second_questions))).all() == sources


def test_delete_item_cascades_storage_without_deleting_source_question(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0005_onboarding_state")
    seed_legacy(conn)
    conn.commit()
    command.upgrade(config, "head")
    answer = conn.execute(select(t.quiz_answers)).mappings().first()
    question_id = conn.scalar(select(t.quiz_items.c.question_id).where(t.quiz_items.c.id == answer["quiz_item_id"]))
    source_ids = set(conn.scalars(select(t.question_sources.c.id).where(t.question_sources.c.question_id == question_id)))
    conn.execute(delete(t.quiz_attempts).where(t.quiz_attempts.c.quiz_item_id == answer["quiz_item_id"]))
    conn.execute(delete(t.quiz_items).where(t.quiz_items.c.id == answer["quiz_item_id"]))
    conn.commit()
    assert conn.scalar(select(t.quiz_answers.c.id).where(t.quiz_answers.c.id == answer["id"])) is None
    assert conn.scalar(select(t.quiz_answer_heads.c.answer_id).where(t.quiz_answer_heads.c.answer_id == answer["id"])) is None
    assert conn.scalar(select(t.quiz_grading_jobs.c.id).where(t.quiz_grading_jobs.c.answer_id == answer["id"])) is None
    assert conn.scalar(select(t.questions.c.id).where(t.questions.c.id == question_id)) == question_id
    assert set(conn.scalars(select(t.question_sources.c.id).where(t.question_sources.c.question_id == question_id))) == source_ids


def test_revision_uniqueness_and_checks_are_enforced(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0005_onboarding_state")
    seed_legacy(conn)
    conn.commit()
    command.upgrade(config, "head")
    answer = dict(conn.execute(select(t.quiz_answers)).mappings().first())
    for changes in ({}, {"revision": 0}, {"revision": 2, "confidence": "unknown"},
                    {"revision": 2, "choice": -1}, {"revision": 2, "answer_hash": "short"}):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(t.quiz_answers.insert().values(**{**answer, "id": str(uuid4()), **changes}))
    job = dict(conn.execute(select(t.quiz_grading_jobs)).mappings().first())
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(t.quiz_grading_jobs.insert().values(**{**job, "id": str(uuid4())}))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(t.quiz_grading_jobs).where(t.quiz_grading_jobs.c.id == job["id"])
                     .values(status="running"))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(t.quiz_submission_states).values(
            result_snapshot={"ok": True}, result_schema_version="1", result_captured_at=datetime.now(timezone.utc)))


def test_legacy_ownership_mismatch_stops_before_ddl(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0005_onboarding_state")
    first, second = seed_legacy(conn), seed_legacy(conn)
    quiz_id = conn.scalar(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id == first))
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz_id).values(user_id=second))
    conn.commit()
    with pytest.raises(RuntimeError, match="소유권 불일치"):
        command.upgrade(config, "head")
    conn.rollback()
    assert "quiz_answers" not in inspect(conn).get_table_names()
    assert conn.scalar(select(t.quiz_sessions.c.user_id).where(t.quiz_sessions.c.id == quiz_id)) == second
