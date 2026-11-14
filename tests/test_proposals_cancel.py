from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, select, update
from sqlalchemy.sql.dml import Update

from app.core.responses import ApiError
from app.db import schema as t
from app.db.migrations import migration_config
from app.services.proposals import PlanEffect, ProposalService
from tests.test_proposal_storage import DAY, NOW, owner
from tests.test_proposal_storage_migration import legacy_history
from tests.test_proposals import TARGET, assert_api, create, domain, plan


def test_cancel_ignores_freshness_and_replays_without_changing_first_decision(domain_connection, owner):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    operation = domain()
    identifier = create(service, owner, operation)["previewId"]
    before = service.repository.get(owner, identifier)
    plan(conn, owner, revision=10)
    conn.execute(update(t.users).where(t.users.c.id == owner).values(demo_date=DAY + timedelta(days=20)))
    conn.commit()
    service.clock = lambda: NOW + timedelta(days=20)

    def forbidden(*_):
        pytest.fail("cancellation must not inspect current bases or apply domain changes")

    operation = replace(operation, inspect_basis=forbidden, apply_changes=forbidden)
    assert service.cancel(owner, identifier, domain=operation, target=TARGET) == {"cancelled": True}
    first = service.repository.get(owner, identifier)
    assert first["status"] == "cancelled" and first["state_revision"] == 1
    assert first["decided_at"] == NOW + timedelta(days=20)
    assert first["request_status"] == before["request_status"] == "ready"
    assert first["accepted_proposal_id"] is None and first["result_response"] is None
    conn.commit()
    service.clock = lambda: NOW + timedelta(days=90)
    assert service.cancel(owner, identifier, domain=operation, target=TARGET) == {"cancelled": True}
    assert service.repository.get(owner, identifier) == first
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == owner)) == 10
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"


def test_cancel_preserves_sibling_approval_and_original_cancel_on_later_retry(domain_connection, owner):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    first = create(service, owner)["previewId"]
    request_id = service.repository.get(owner, first)["request_id"]
    second = create(service, owner, request_id=request_id)["previewId"]
    assert service.cancel(owner, first, domain=domain(), target=TARGET) == {"cancelled": True}
    cancelled = service.repository.get(owner, first)
    assert service.repository.get(owner, second)["status"] == "ready"
    assert service.apply(owner, second, domain=domain(), target=TARGET)["applied"] is True
    assert service.repository.get(owner, second)["status"] == "applied"
    assert service.cancel(owner, first, domain=domain(), target=TARGET) == {"cancelled": True}
    again = service.repository.get(owner, first)
    assert again["decided_at"] == cancelled["decided_at"] and again["state_revision"] == 1
    assert again["request_status"] == "accepted" and again["accepted_proposal_id"] == second


@pytest.mark.parametrize("terminal", ["applied", "stale"])
def test_cancel_cannot_relabel_other_terminal_states(domain_connection, owner, terminal):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner)["previewId"]
    if terminal == "applied":
        service.apply(owner, identifier, domain=domain(), target=TARGET)
    else:
        service.repository.mark_stale(owner, identifier, expected_revision=0, now=NOW)
    before = service.repository.get(owner, identifier)
    with pytest.raises(ApiError) as error:
        service.cancel(owner, identifier, domain=domain(), target=TARGET)
    assert_api(error, "PREVIEW_STALE", 409)
    assert service.repository.get(owner, identifier) == before


@pytest.mark.parametrize("mismatch", ["missing_id", "absent", "foreign", "kind", "target"])
def test_cancel_keeps_identifier_and_ownership_boundaries(domain_connection, owner, mismatch):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner)["previewId"]
    user, target, operation = owner, TARGET, domain()
    if mismatch == "missing_id":
        identifier = None
    elif mismatch == "absent":
        identifier = str(uuid4())
    elif mismatch == "foreign":
        user = str(uuid4())
        conn.execute(t.users.insert().values(id=user, created_at=NOW, updated_at=NOW))
    elif mismatch == "kind":
        operation = replace(operation, kind="other")
    else:
        target = {"type": "another"}
    with pytest.raises(ApiError) as error:
        service.cancel(user, identifier, domain=operation, target=target)
    assert_api(error, "PREVIEW_REQUIRED" if mismatch == "missing_id" else "NO_PREVIEW",
               409 if mismatch == "missing_id" else 400 if mismatch == "absent" else 404)


def test_cancel_cas_failure_and_caller_rollback_preserve_ready_candidate(domain_connection, owner):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner)["previewId"]
    conn.commit()
    before = service.repository.get(owner, identifier)
    armed = [True]

    def conflict(connection, clause, *_):
        if armed[0] and isinstance(clause, Update) and clause.table is t.plan_proposal_states:
            armed[0] = False
            connection.execute(update(t.plan_proposal_states).where(t.plan_proposal_states.c.proposal_id == identifier)
                               .values(state_revision=2))

    event.listen(conn, "before_execute", conflict)
    try:
        with pytest.raises(ApiError) as error:
            service.cancel(owner, identifier, domain=domain(), target=TARGET)
        assert_api(error, "PREVIEW_STALE", 409)
    finally:
        event.remove(conn, "before_execute", conflict)
    assert not armed[0]
    conn.commit()
    assert service.repository.get(owner, identifier) == before
    service.cancel(owner, identifier, domain=domain(), target=TARGET)
    conn.rollback()
    assert service.repository.get(owner, identifier) == before


def test_cancel_never_relabels_legacy_applied_or_incomplete_history(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0006_quiz_answer_storage")
    user, plan_id, identifiers = legacy_history(conn)
    command.upgrade(config, "head")
    service = ProposalService(conn, clock=lambda: NOW)
    operation = domain(effect=PlanEffect.UPDATE, kind="redistribute")
    for identifier in (identifiers[0], identifiers[2]):
        before = service.repository.get(user, identifier)
        with pytest.raises(ApiError) as error:
            service.cancel(user, identifier, domain=operation, target={"type": "plan", "id": plan_id})
        assert_api(error, "PREVIEW_STALE", 409)
        assert service.repository.get(user, identifier) == before
