from datetime import date, datetime, timezone
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, null, select, update
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.db.migrations import migration_config
from app.seed.loader import load_seed
from tests.test_quiz_storage_migration import rows, seed_legacy

NOW = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)
DAY = date(2026, 11, 14)


def legacy_history(conn):
    user = seed_legacy(conn)
    plan_id = conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id == user))
    proposal_ids = []
    for status in ("accepted", "ready", "rejected", "stale", "needs_input"):
        request_id = str(uuid4())
        conn.execute(t.plan_change_requests.insert().values(
            id=request_id, user_id=user, kind="redistribute", status=status,
            base_plan_id=plan_id, base_plan_revision=1, input_payload={"raw": "  원문  ", "nullable": None},
            request_text="  시간이 부족해요  ", followup_question="어느 날인가요?", followup_answer="금요일",
            effective_date=date(2026, 11, 15), created_at=NOW, updated_at=NOW,
            decided_at=NOW if status == "accepted" else None))
        if status == "needs_input":
            continue
        for position in range(2 if status == "accepted" else 1):
            proposal_id = str(uuid4())
            proposal_ids.append(proposal_id)
            conn.execute(t.plan_proposals.insert().values(
                id=proposal_id, request_id=request_id, proposal_type="revision", description="과거 후보",
                changes={"plannedTasks": [], "note": "정확한 과거 변경안", "position": position}, created_at=NOW))
            if status == "accepted" and position == 0:
                conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == request_id)
                             .values(accepted_proposal_id=proposal_id))
    conn.commit()
    return user, plan_id, proposal_ids


def test_preview_upgrade_keeps_legacy_plan_dialogue_and_selection_facts(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0006_quiz_answer_storage")
    user, plan_id, proposal_ids = legacy_history(conn)
    old_tables = (t.plans, t.plan_change_requests, t.plan_proposals, t.onboarding_messages,
                  t.study_tasks, t.study_records, t.calendar_events)
    before = {table.name: rows(conn, table) for table in old_tables}
    conn.commit()
    for _ in range(2):
        command.upgrade(config, "head")
        states = rows(conn, t.plan_proposal_states)
        assert len(states) == len(proposal_ids)
        assert [row["proposal_id"] for row in states if row["status"] == "applied"] == [proposal_ids[0]]
        for state in states:
            assert state["user_id"] == user and state["kind"] == "redistribute" and state["legacy"]
            assert (state["base_plan_id"], state["base_plan_revision"], state["state_revision"]) == (plan_id, 1, 0)
            assert state["status"] in ("legacy", "applied")
            if state["status"] == "applied":
                assert state["decided_at"].replace(tzinfo=timezone.utc) == NOW
            else:
                assert state["decided_at"] is None
            for name in ("target", "normalized_input", "input_hash", "base_date", "base_structure_hash",
                         "dependency_versions", "preview_response", "preview_schema_version", "result_response",
                         "result_schema_version", "result_captured_at"):
                assert state[name] is None
                assert conn.scalar(select(t.plan_proposal_states.c.proposal_id).where(
                    t.plan_proposal_states.c.proposal_id == state["proposal_id"],
                    t.plan_proposal_states.c[name].is_(None))) == state["proposal_id"]
            original = next(row for row in before["plan_proposals"] if row["id"] == state["proposal_id"])
            assert state["legacy_snapshot"]["proposal"]["changes"] == original["changes"]
            assert state["legacy_snapshot"]["request"]["input_payload"] == {"raw": "  원문  ", "nullable": None}
        for table in old_tables:
            assert rows(conn, table) == before[table.name]
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_server_default": True}), t.metadata) == []
        assert MigrationContext.configure(conn).get_current_heads() == tuple(ScriptDirectory.from_config(config).get_heads())
        conn.commit()
        command.downgrade(config, "0006_quiz_answer_storage")
        assert "plan_proposal_states" not in inspect(conn).get_table_names()
        for table in old_tables:
            assert rows(conn, table) == before[table.name]
        conn.commit()


def valid_state(user, plan_id, proposal_id):
    return dict(proposal_id=proposal_id, user_id=user, kind="redistribute", target={"type": "plan", "id": plan_id},
                normalized_input={}, input_hash="a" * 64, base_date=DAY, base_plan_id=plan_id, base_plan_revision=1,
                base_structure_hash=None, dependency_versions={}, status="ready", state_revision=0,
                preview_response={"previewId": proposal_id}, preview_schema_version=1,
                result_response=null(), result_schema_version=None, result_captured_at=None, decided_at=None,
                legacy=False, legacy_snapshot=null())


@pytest.mark.parametrize("values", [
    {"base_date": None}, {"target": null()}, {"normalized_input": null()}, {"dependency_versions": null()},
    {"input_hash": None}, {"input_hash": "short"}, {"base_plan_revision": 0},
    {"base_plan_id": None, "base_plan_revision": None}, {"base_structure_hash": "a" * 64},
    {"preview_response": null()}, {"preview_schema_version": 0}, {"legacy_snapshot": {}},
    {"status": "applied"}, {"status": "unknown"}, {"status": "legacy"}, {"state_revision": -1},
    {"decided_at": NOW}, {"result_response": {"applied": True}},
])
def test_preview_schema_rejects_incomplete_new_ready_or_invalid_transition(domain_connection, values):
    conn = domain_connection
    user, plan_id, proposal_ids = legacy_history(conn)
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(t.plan_proposal_states.insert().values(**{**valid_state(user, plan_id, proposal_ids[2]), **values}))
    assert conn.scalar(select(t.plan_proposal_states.c.proposal_id)) is None


def test_preview_schema_accepts_initial_structure_basis_without_plan(domain_connection):
    conn = domain_connection
    user, plan_id, proposal_ids = legacy_history(conn)
    values = valid_state(user, plan_id, proposal_ids[2])
    values.update(base_plan_id=None, base_plan_revision=None, base_structure_hash="b" * 64)
    conn.execute(t.plan_proposal_states.insert().values(**values))
    state = conn.execute(select(t.plan_proposal_states)).mappings().one()
    assert state["base_plan_id"] is None and state["base_structure_hash"] == "b" * 64


def test_preview_cascade_respects_actual_demo_wipe_order_and_other_user_history(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0006_quiz_answer_storage")
    first, _, _ = legacy_history(conn)
    second, _, _ = legacy_history(conn)
    command.upgrade(config, "head")
    second_states = [row for row in rows(conn, t.plan_proposal_states) if row["user_id"] == second]
    second_requests = [row for row in rows(conn, t.plan_change_requests) if row["user_id"] == second]
    second_plan = conn.execute(select(t.plans).where(t.plans.c.user_id == second)).mappings().one()
    conn.commit()
    load_seed(conn, first, "empty")
    conn.commit()
    assert rows(conn, t.plan_proposal_states) == second_states
    assert rows(conn, t.plan_change_requests) == second_requests
    assert conn.execute(select(t.plans).where(t.plans.c.user_id == second)).mappings().one() == second_plan
    assert conn.scalar(select(t.users.c.id).where(t.users.c.id == first)) == first
