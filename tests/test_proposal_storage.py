from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.repositories.proposals import (
    InvalidProposalJSON, LegacyProposalUnavailable, ProposalConflict, ProposalNotFound,
    ProposalRepository, canonical_hash,
)

NOW = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)
DAY = date(2026, 11, 14)


@pytest.fixture
def owner(domain_connection):
    user = str(uuid4())
    domain_connection.execute(t.users.insert().values(id=user, created_at=NOW, updated_at=NOW))
    domain_connection.commit()
    return user


def candidate(conn, user, **overrides):
    proposal_id = str(uuid4())
    values = dict(kind="initial", target={"type": "user", "id": user},
                  normalized_input={"request": "  원문  ", "goals": [None, 90]}, base_date=DAY,
                  base_structure_hash=canonical_hash({"materials": [], "goals": [], "schedules": []}),
                  dependency_versions={"policy": "allocation-v1"}, changes={"plannedTasks": [], "goal": None},
                  preview_response={"previewId": proposal_id, "summary": {"pages": 0}},
                  now=NOW, proposal_type="initial", description="첫 계획 후보", proposal_id=proposal_id)
    return ProposalRepository(conn).create(user, **{**values, **overrides})


def counts(conn):
    return {table.name: conn.scalar(select(func.count()).select_from(table)) for table in
            (t.plan_change_requests, t.plan_proposals, t.plan_proposal_states, t.plans, t.study_tasks, t.calendar_events)}


def test_candidate_creation_preserves_basis_and_does_not_approve(domain_connection, owner):
    conn = domain_connection
    stored = candidate(conn, owner)
    assert stored["status"] == stored["request_status"] == "ready" and stored["state_revision"] == 0
    assert stored["base_plan_id"] is stored["base_plan_revision"] is stored["result_response"] is None
    assert stored["normalized_input"] == {"request": "  원문  ", "goals": [None, 90]}
    assert stored["input_hash"] == canonical_hash(stored["normalized_input"])
    assert stored["base_date"] == DAY and stored["dependency_versions"] == {"policy": "allocation-v1"}
    assert stored["preview_response"]["previewId"] == stored["proposal_id"]
    assert counts(conn) == {"plan_change_requests": 1, "plan_proposals": 1, "plan_proposal_states": 1,
                            "plans": 0, "study_tasks": 0, "calendar_events": 0}
    assert conn.scalar(select(t.users.c.updated_at).where(t.users.c.id == owner)).replace(tzinfo=timezone.utc) == NOW
    conn.commit()
    # Reconnect with the same isolated PG schema, or the same SQLite file.
    schema = conn.exec_driver_sql("select current_schema()").scalar() if conn.dialect.name == "postgresql" else None
    with conn.engine.connect() as reopened:
        if schema:
            reopened.exec_driver_sql(f'SET search_path TO "{schema}"')
        assert ProposalRepository(reopened).get(owner, stored["proposal_id"]) == stored
        found = ProposalRepository(reopened).ready(owner, kind="initial", input_hash=stored["input_hash"])
        assert found == [stored]  # no clock-based 30-minute expiration


def test_create_uses_explicit_owned_plan_basis_without_changing_revision(domain_connection, owner):
    conn = domain_connection
    plan_id = str(uuid4())
    conn.execute(t.plans.insert().values(id=plan_id, user_id=owner, revision=7, created_at=NOW, approved_at=NOW))
    before = dict(conn.execute(select(t.plans)).mappings().one())
    stored = candidate(conn, owner, kind="redistribute", base_plan_id=plan_id, base_plan_revision=7,
                       base_structure_hash=None, now=NOW + timedelta(days=90))
    assert stored["base_plan_id"] == plan_id and stored["base_plan_revision"] == 7
    assert stored["base_structure_hash"] is None
    assert dict(conn.execute(select(t.plans)).mappings().one()) == before


def test_create_and_read_enforce_request_owner_kind_and_basis(domain_connection, owner):
    conn = domain_connection
    first = candidate(conn, owner)
    other = str(uuid4())
    conn.execute(t.users.insert().values(id=other, created_at=NOW, updated_at=NOW))
    conn.commit()
    with pytest.raises(ProposalNotFound) as exc:
        ProposalRepository(conn).get(other, first["proposal_id"])
    assert exc.value.foreign
    with pytest.raises(ProposalNotFound):
        candidate(conn, other, request_id=first["request_id"])
    with pytest.raises(ProposalConflict, match="kind or basis"):
        candidate(conn, owner, request_id=first["request_id"], kind="rest_today")
    second = candidate(conn, owner, request_id=first["request_id"], proposal_type="revision")
    assert first["request_id"] == second["request_id"]
    assert counts(conn)["plan_change_requests"] == 1
    conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == first["request_id"])
                 .values(status="rejected"))
    with pytest.raises(ProposalConflict, match="ready"):
        candidate(conn, owner, request_id=first["request_id"])
    conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == first["request_id"])
                 .values(kind="regenerate"))
    with pytest.raises(ProposalConflict, match="kind"):
        ProposalRepository(conn).get(owner, first["proposal_id"])


def test_candidate_outer_rollback_and_insert_failure_leave_no_orphans(domain_connection, owner):
    conn = domain_connection
    before = counts(conn)
    conn.commit()
    stored = candidate(conn, owner)
    conn.rollback()
    assert counts(conn) == before
    stored = candidate(conn, owner)
    before = counts(conn)
    with pytest.raises(IntegrityError):
        candidate(conn, owner, proposal_id=stored["proposal_id"])
    assert counts(conn) == before
    assert ProposalRepository(conn).get(owner, stored["proposal_id"]) == stored


@pytest.mark.parametrize("field", ["target", "normalized_input", "dependency_versions", "changes", "preview_response"])
def test_invalid_json_is_rejected_before_any_database_statement(domain_connection, owner, field):
    conn = domain_connection
    circular = {}
    circular["self"] = circular
    invalid = [{"x": float("nan")}, {"x": float("inf")}, {"x": float("-inf")}, {1: "key"},
               {"x": "\x00"}, {"\x00": "value"}, {"x": "\ud800"}, {"\udfff": "value"}, circular,
               '{"same":1,"same":2}', '{"nested":{"a":1,"\\u0061":2}}']
    def unexpected_statement(*_args):
        pytest.fail("invalid JSON reached the database")
    event.listen(conn, "before_cursor_execute", unexpected_statement)
    try:
        for value in invalid:
            with pytest.raises(InvalidProposalJSON):
                candidate(conn, owner, **{field: value})
    finally:
        event.remove(conn, "before_cursor_execute", unexpected_statement)
    assert all(value == 0 for value in counts(conn).values())


@pytest.mark.parametrize("values", [
    {"base_date": None}, {"base_date": NOW}, {"now": NOW.replace(tzinfo=None)},
    {"base_structure_hash": None}, {"base_structure_hash": "x" * 64}, {"base_plan_revision": 1},
    {"target": {}}, {"preview_schema_version": True}, {"preview_schema_version": 0}, {"kind": " "},
])
def test_creation_rejects_missing_or_invalid_basis_without_rows(domain_connection, owner, values):
    with pytest.raises(ValueError):
        candidate(domain_connection, owner, **values)
    assert all(value == 0 for value in counts(domain_connection).values())


def test_old_writer_candidate_without_state_is_explicitly_unavailable(domain_connection, owner):
    conn = domain_connection
    stored = candidate(conn, owner)
    conn.execute(t.plan_proposal_states.delete().where(t.plan_proposal_states.c.proposal_id == stored["proposal_id"]))
    with pytest.raises(LegacyProposalUnavailable):
        ProposalRepository(conn).get(owner, stored["proposal_id"])
