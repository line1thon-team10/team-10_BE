from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

from alembic import command
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.sql.dml import Update

from app.db import schema as t
from app.db.migrations import migration_config
from app.repositories.locking import UserBusyError
from app.repositories.proposals import (
    InvalidProposalJSON, LegacyProposalUnavailable, LegacyResponseUnavailable,
    ProposalConflict, ProposalRepository,
)
from tests.test_proposal_storage import NOW, candidate, owner
from tests.test_proposal_storage_migration import legacy_history


def test_first_result_and_selection_are_atomic_and_replay_is_immutable(domain_connection, owner):
    conn = domain_connection
    stored = candidate(conn, owner)
    repo = ProposalRepository(conn)
    at = NOW + timedelta(days=90)  # no arbitrary 30-minute expiration in storage
    first = repo.mark_applied(owner, stored["proposal_id"], expected_revision=0,
                              response={"applied": True, "nullable": None}, now=at)
    assert first.changed
    assert first.proposal["status"] == "applied" and first.proposal["request_status"] == "accepted"
    assert first.proposal["accepted_proposal_id"] == stored["proposal_id"]
    assert first.proposal["state_revision"] == 1
    assert first.proposal["decided_at"] == first.proposal["result_captured_at"] == at
    assert first.proposal["result_schema_version"] == 1
    conn.commit()
    repeated = repo.mark_applied(owner, stored["proposal_id"], expected_revision=0,
                                 response={"a different": "response"}, schema_version=2, now=at + timedelta(days=1))
    assert not repeated.changed and repeated.proposal == first.proposal
    assert repo.result(owner, stored["proposal_id"]) == {"applied": True, "nullable": None}
    assert repeated.proposal["changes"] == stored["changes"]
    assert repeated.proposal["preview_response"] == stored["preview_response"]
    assert conn.scalar(select(t.plans.c.id)) is None


@pytest.mark.parametrize("method,status", [("cancel", "cancelled"), ("mark_stale", "stale")])
def test_cancelled_or_stale_candidates_cannot_be_applied_or_relabelled(domain_connection, owner, method, status):
    conn = domain_connection
    stored = candidate(conn, owner)
    repo = ProposalRepository(conn)
    changed = getattr(repo, method)(owner, stored["proposal_id"], expected_revision=0, now=NOW)
    assert changed.changed and changed.proposal["status"] == status
    assert changed.proposal["request_status"] == "ready" and changed.proposal["accepted_proposal_id"] is None
    assert changed.proposal["result_response"] is None
    for terminal in ("cancel", "mark_stale"):
        with pytest.raises(ProposalConflict):
            getattr(repo, terminal)(owner, stored["proposal_id"], expected_revision=1, now=NOW)
    with pytest.raises(ProposalConflict):
        repo.mark_applied(owner, stored["proposal_id"], expected_revision=1, response={"applied": True}, now=NOW)
    assert repo.get(owner, stored["proposal_id"]) == changed.proposal


def test_only_one_proposal_per_request_can_be_selected(domain_connection, owner):
    conn = domain_connection
    first = candidate(conn, owner)
    second = candidate(conn, owner, request_id=first["request_id"], changes={"different": "saved candidate"})
    repo = ProposalRepository(conn)
    selected = repo.mark_applied(owner, first["proposal_id"], expected_revision=0, response={"applied": True}, now=NOW)
    with pytest.raises(ProposalConflict, match="already won"):
        repo.mark_applied(owner, second["proposal_id"], expected_revision=0, response={"applied": True}, now=NOW)
    assert repo.get(owner, second["proposal_id"])["status"] == "ready"
    assert repo.get(owner, first["proposal_id"]) == selected.proposal
    assert repo.ready(owner, kind="initial") == []


def test_state_cas_loss_rolls_back_the_request_selection(domain_connection, owner):
    conn = domain_connection
    stored = candidate(conn, owner)
    armed = [True]
    def change_revision_before_cas(connection, clause, _multiparams, _params, _options):
        if armed[0] and isinstance(clause, Update) and clause.table is t.plan_proposal_states:
            armed[0] = False
            connection.execute(update(t.plan_proposal_states).where(
                t.plan_proposal_states.c.proposal_id == stored["proposal_id"]).values(state_revision=1))
    event.listen(conn, "before_execute", change_revision_before_cas)
    try:
        with pytest.raises(ProposalConflict, match="compare-and-set"):
            ProposalRepository(conn).mark_applied(owner, stored["proposal_id"], expected_revision=0,
                                                 response={"applied": True}, now=NOW)
    finally:
        event.remove(conn, "before_execute", change_revision_before_cas)
    assert not armed[0]
    assert ProposalRepository(conn).get(owner, stored["proposal_id"]) == stored


@pytest.mark.parametrize("fail", [False, True])
def test_caller_rollback_covers_domain_write_selection_and_result(domain_connection, owner, fail):
    conn = domain_connection
    stored = candidate(conn, owner)
    conn.commit()
    with pytest.raises(ProposalConflict if fail else RuntimeError,
                       match="state revision" if fail else "caller domain execution"):
        with conn.begin():
            conn.execute(update(t.users).where(t.users.c.id == owner).values(onboarding_status="completed"))
            ProposalRepository(conn).mark_applied(owner, stored["proposal_id"], expected_revision=99 if fail else 0,
                                                 response={"applied": True}, now=NOW)
            raise RuntimeError("caller domain execution failed")
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"
    assert ProposalRepository(conn).get(owner, stored["proposal_id"]) == stored


def test_invalid_result_json_never_reaches_database(domain_connection, owner):
    conn = domain_connection
    stored = candidate(conn, owner)
    circular = {}
    circular["self"] = circular
    invalid = [{"x": float("nan")}, {"x": float("inf")}, {"x": float("-inf")}, {1: "key"},
               {"x": "\x00"}, {"\x00": "value"}, {"x": "\ud800"}, {"\udfff": "value"},
               '{"same":1,"same":2}', circular]
    def unexpected_statement(*_args):
        pytest.fail("invalid result JSON reached the database")
    event.listen(conn, "before_cursor_execute", unexpected_statement)
    try:
        for value in invalid:
            with pytest.raises(InvalidProposalJSON):
                ProposalRepository(conn).mark_applied(owner, stored["proposal_id"], expected_revision=0,
                                                     response=value, now=NOW)
    finally:
        event.remove(conn, "before_cursor_execute", unexpected_statement)
    assert ProposalRepository(conn).get(owner, stored["proposal_id"]) == stored


@pytest.mark.parametrize("values", [{"expected_revision": -1}, {"expected_revision": True},
                                    {"expected_revision": 1.0}, {"now": NOW - timedelta(seconds=1)},
                                    {"now": NOW.replace(tzinfo=None)}, {"schema_version": 0}])
def test_invalid_transition_metadata_does_not_change_state(domain_connection, owner, values):
    conn = domain_connection
    stored = candidate(conn, owner)
    with pytest.raises(ValueError):
        ProposalRepository(conn).mark_applied(owner, stored["proposal_id"], **{
            "expected_revision": 0, "response": {"applied": True}, "now": NOW, **values})
    assert ProposalRepository(conn).get(owner, stored["proposal_id"]) == stored


def test_legacy_applied_without_response_is_distinct_from_unusable_candidate(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0006_quiz_answer_storage")
    user, _, proposal_ids = legacy_history(conn)
    command.upgrade(config, "head")
    repo = ProposalRepository(conn)
    for identifier, expected in [(proposal_ids[0], LegacyResponseUnavailable), (proposal_ids[2], LegacyProposalUnavailable)]:
        before = repo.get(user, identifier)
        with pytest.raises(expected):
            repo.result(user, identifier)
        with pytest.raises(expected):
            repo.mark_applied(user, identifier, expected_revision=0, response={"applied": True}, now=NOW)
        assert repo.get(user, identifier) == before


@pytest.mark.parametrize("same_candidate", [False, True])
def test_concurrent_selection_replays_same_candidate_or_rejects_sibling(domain_connection, owner, same_candidate):
    conn = domain_connection
    first = candidate(conn, owner)
    second = first if same_candidate else candidate(conn, owner, request_id=first["request_id"])
    schema = conn.exec_driver_sql("select current_schema()").scalar() if conn.dialect.name == "postgresql" else None
    conn.commit()
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("PRAGMA busy_timeout=20")
        conn.commit()
    entered, release = Event(), Event()

    def winner():
        with conn.engine.connect() as connection:
            if schema:
                connection.exec_driver_sql(f'SET search_path TO "{schema}"')
            result = ProposalRepository(connection).mark_applied(owner, first["proposal_id"], expected_revision=0,
                                                                  response={"winner": True}, now=NOW)
            entered.set()
            assert release.wait(5)
            connection.commit()
            return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(winner)
        try:
            assert entered.wait(5)
            with pytest.raises(UserBusyError):
                ProposalRepository(conn).mark_applied(owner, second["proposal_id"], expected_revision=0,
                                                      response={"winner": False}, now=NOW)
            conn.rollback()
        finally:
            release.set()
        chosen = future.result(timeout=5)
    if same_candidate:
        retry = ProposalRepository(conn).mark_applied(owner, second["proposal_id"], expected_revision=0,
                                                     response={"winner": False}, now=NOW)
        assert not retry.changed and retry.proposal == chosen.proposal
    else:
        with pytest.raises(ProposalConflict):
            ProposalRepository(conn).mark_applied(owner, second["proposal_id"], expected_revision=0,
                                                  response={"winner": False}, now=NOW)
        assert ProposalRepository(conn).get(owner, second["proposal_id"])["status"] == "ready"
    assert ProposalRepository(conn).result(owner, first["proposal_id"]) == {"winner": True}
