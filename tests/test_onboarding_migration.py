from datetime import date, datetime, timezone
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.db.migrations import migration_config
from migrations.baselines import schema_0001 as old

CONFIRMATIONS = (
    "onboarding_exam_date_confirmed_at", "onboarding_goal_confirmed_at",
    "onboarding_goal_total_confirmed_at", "onboarding_goal_etc_confirmed_at",
)
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def test_legacy_confirmation_is_not_guessed_and_roundtrip_keeps_data(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0004_focus_contract")
    user_id, subject_id, message_id = (str(uuid4()) for _ in range(3))
    conn.execute(old.users.insert().values(id=user_id, onboarding_status="completed",
                                         created_at=NOW, updated_at=NOW))
    conn.execute(old.subjects.insert().values(
        id=subject_id, user_id=user_id, code="DB", name="데이터베이스", color="#123456", status="active",
        exam_date=date(2026, 12, 1), target_grade="A+", passing_criteria="90점", goal_confirmed_at=NOW,
        created_at=NOW, updated_at=NOW))
    conn.execute(old.onboarding_messages.insert().values(
        id=message_id, user_id=user_id, role="user", text="A+를 목표로 해요", chips=None, created_at=NOW))
    request_id, linked_message_id = str(uuid4()), str(uuid4())
    conn.execute(old.plan_change_requests.insert().values(
        id=request_id, user_id=user_id, kind="subject_add", status="needs_input", input_payload={"subjectId": subject_id},
        created_at=NOW, updated_at=NOW))
    conn.execute(old.onboarding_messages.insert().values(
        id=linked_message_id, user_id=user_id, change_request_id=request_id, role="ai", text="새 과목의 시험일은요?",
        chips=["다음 주"], created_at=NOW))
    conn.commit()

    for _ in range(2):
        command.upgrade(config, "head")
        subject = conn.execute(select(t.subjects)).mappings().one()
        assert subject["id"] == subject_id and subject["status"] == "active"
        assert subject["exam_date"] == date(2026, 12, 1)
        assert subject["target_grade"] == "A+" and subject["passing_criteria"] == "90점"
        assert subject["goal_confirmed_at"] is not None
        assert all(subject[name] is None for name in CONFIRMATIONS)
        message = conn.execute(select(t.onboarding_messages).where(t.onboarding_messages.c.id == message_id)).mappings().one()
        assert message["id"] == message_id and message["text"] == "A+를 목표로 해요"
        assert message["subject_id"] is None and message["sort_order"] is None
        linked = conn.execute(select(t.onboarding_messages).where(t.onboarding_messages.c.id == linked_message_id)).mappings().one()
        assert linked["change_request_id"] == request_id and linked["chips"] == ["다음 주"]
        assert linked["subject_id"] is None  # request payload is not guessed into a new context
        assert conn.scalar(select(t.users.c.onboarding_status)) == "completed"
        context = MigrationContext.configure(conn, opts={"compare_server_default": True})
        assert compare_metadata(context, t.metadata) == []
        conn.commit()

    command.downgrade(config, "0004_focus_contract")
    assert conn.scalar(select(old.subjects.c.id)) == subject_id
    assert set(conn.scalars(select(old.onboarding_messages.c.id))) == {message_id, linked_message_id}
    assert not set(CONFIRMATIONS) & {column["name"] for column in inspect(conn).get_columns("subjects")}
    conn.commit()
    command.upgrade(config, "head")
    assert conn.scalar(select(t.subjects.c.onboarding_goal_etc_confirmed_at)) is None


def test_draft_message_subject_reference_is_enforced(domain_connection):
    conn = domain_connection
    user_id = str(uuid4())
    conn.execute(t.users.insert().values(id=user_id, created_at=NOW, updated_at=NOW))
    conn.commit()
    with pytest.raises(IntegrityError):
        conn.execute(t.onboarding_messages.insert().values(
            id=str(uuid4()), user_id=user_id, subject_id=str(uuid4()), role="ai", text="시험일은 언제인가요?",
            sort_order=1, created_at=NOW))
    conn.rollback()


def test_context_fk_keeps_conversation_until_explicit_deletion(domain_connection):
    conn = domain_connection
    user_id, subject_id, message_id = (str(uuid4()) for _ in range(3))
    conn.execute(t.users.insert().values(id=user_id, created_at=NOW, updated_at=NOW))
    conn.execute(t.subjects.insert().values(
        id=subject_id, user_id=user_id, code="DB", name="데이터베이스", color="#123456",
        created_at=NOW, updated_at=NOW))
    conn.execute(t.onboarding_messages.insert().values(
        id=message_id, user_id=user_id, subject_id=subject_id, role="ai", text="시험일은요?", sort_order=1,
        created_at=NOW))
    conn.commit()
    with pytest.raises(IntegrityError):
        conn.execute(delete(t.subjects).where(t.subjects.c.id == subject_id))
    conn.rollback()
    assert conn.scalar(select(t.onboarding_messages.c.id)) == message_id
    conn.execute(delete(t.onboarding_messages).where(t.onboarding_messages.c.id == message_id))
    conn.execute(delete(t.subjects).where(t.subjects.c.id == subject_id))
    conn.commit()
    assert conn.execute(select(t.subjects)).first() is None
