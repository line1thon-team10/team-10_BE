from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.sql.dml import Update
from uuid import UUID

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import lock_user
from app.repositories.proposals import ProposalRepository, canonical_hash
from app.services.proposals import DomainBasis, PlanEffect, ProposalService
from tests.test_proposal_storage import NOW, owner
from tests.test_proposals import TARGET, assert_api, create, domain, plan


def connection_scope(conn):
    schema = conn.exec_driver_sql("select current_schema()").scalar() if conn.dialect.name == "postgresql" else None
    if schema is None:
        conn.exec_driver_sql("PRAGMA busy_timeout=20")
    conn.commit()
    return schema


@pytest.mark.parametrize("effect", list(PlanEffect))
@pytest.mark.parametrize("relation", ["same", "sibling", "separate_request"])
def test_concurrent_apply_replays_one_candidate_or_rejects_a_competing_basis(domain_connection, owner, effect, relation):
    conn = domain_connection
    if effect is PlanEffect.UPDATE:
        plan(conn, owner)
    calls = []

    def inspect_basis(connection, context, target):
        # Draft domains own a meaningful version/structure, even without a plan.
        current = {"status": context.user.onboarding_status}
        return DomainBasis(current, structure_hash=canonical_hash(current) if context.plan is None else None)

    def execute(connection, context, changes):
        calls.append(context.now)
        if context.plan:
            assert connection.scalar(select(t.plans.c.id).where(t.plans.c.user_id == owner)) == context.plan.id
        connection.execute(update(t.users).where(t.users.c.id == owner).values(onboarding_status=changes["status"]))
        return {"applied": True, "at": context.now.isoformat(), "planId": context.plan.id if context.plan else None}

    operation = domain(effect=effect, inspect=inspect_basis, apply=execute)
    service = ProposalService(conn, clock=lambda: NOW)
    first = create(service, owner, operation)["previewId"]
    request_id = service.repository.get(owner, first)["request_id"]
    second = first if relation == "same" else create(service, owner, operation,
        **({"request_id": request_id} if relation == "sibling" else {}))["previewId"]
    schema = connection_scope(conn)
    entered, release = Event(), Event()

    def winner():
        with conn.engine.connect() as writer:
            if schema:
                writer.exec_driver_sql(f'SET search_path TO "{schema}"')
            result = ProposalService(writer, clock=lambda: NOW).apply(owner, first, domain=operation, target=TARGET)
            entered.set()
            assert release.wait(5)
            writer.commit()
            return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(winner)
        try:
            assert entered.wait(5)
            with pytest.raises(ApiError) as error:
                service.apply(owner, second, domain=operation, target=TARGET)
            assert_api(error, "REQUEST_IN_PROGRESS", 409)
            conn.rollback()
        finally:
            release.set()
        result = future.result(timeout=5)
    service.clock = lambda: NOW + timedelta(seconds=1)
    if relation == "same":
        assert service.apply(owner, first, domain=operation, target=TARGET) == result
        stored = service.repository.get(owner, first)
        assert stored["state_revision"] == 1 and stored["result_captured_at"] == NOW
    else:
        with pytest.raises(ApiError) as error:
            service.apply(owner, second, domain=operation, target=TARGET)
        assert_api(error, "PREVIEW_STALE", 409)
        assert service.repository.get(owner, second)["status"] == "ready"
        assert service.repository.get(owner, second)["state_revision"] == 0
    assert calls == [NOW]
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == owner)) == (
        8 if effect is PlanEffect.UPDATE else 1 if effect is PlanEffect.CREATE else None)
    assert service.repository.get(owner, first)["result_response"] == result


@pytest.mark.parametrize("first_action", ["apply", "cancel", "delete"])
def test_cancel_and_owned_deletion_serialize_with_approval(domain_connection, owner, first_action):
    conn = domain_connection
    operation, service = domain(), ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner, operation)["previewId"]
    schema = connection_scope(conn)
    entered, release = Event(), Event()

    def winner():
        with conn.engine.connect() as writer:
            if schema:
                writer.exec_driver_sql(f'SET search_path TO "{schema}"')
            if first_action == "delete":
                lock_user(writer, UUID(owner))
                writer.execute(delete(t.plan_proposals).where(t.plan_proposals.c.id == identifier))
            else:
                method = getattr(ProposalService(writer, clock=lambda: NOW), first_action)
                method(owner, identifier, domain=operation, target=TARGET)
            entered.set()
            assert release.wait(5)
            writer.commit()

    loser = service.cancel if first_action == "apply" else service.apply
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(winner)
        try:
            assert entered.wait(5)
            with pytest.raises(ApiError) as error:
                loser(owner, identifier, domain=operation, target=TARGET)
            assert_api(error, "REQUEST_IN_PROGRESS", 409)
            conn.rollback()
        finally:
            release.set()
        future.result(timeout=5)
    with pytest.raises(ApiError) as error:
        loser(owner, identifier, domain=operation, target=TARGET)
    assert_api(error, "NO_PREVIEW" if first_action == "delete" else "PREVIEW_STALE",
               400 if first_action == "delete" else 409)
    if first_action != "delete":
        stored = service.repository.get(owner, identifier)
        assert stored["status"] == ("applied" if first_action == "apply" else "cancelled")
        assert stored["state_revision"] == 1


@pytest.mark.parametrize("changed_table", ["plan", "candidate"])
def test_postgresql_external_cas_winner_rolls_back_only_this_application(domain_connection, owner, changed_table):
    conn = domain_connection
    if conn.dialect.name != "postgresql":
        pytest.skip("SQLite's database writer lock prevents this independent-row writer race")
    plan(conn, owner, revision=7)
    operation = domain(effect=PlanEffect.UPDATE)
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner, operation)["previewId"]
    schema = connection_scope(conn)
    before = service.repository.get(owner, identifier)
    conn.commit()
    armed = [True]
    table = t.plans if changed_table == "plan" else t.plan_proposal_states

    def external_writer(connection, clause, *_):
        if not armed[0] or not isinstance(clause, Update) or clause.table is not table:
            return
        armed[0] = False
        # A writer bypassing the user-lock convention still cannot defeat CAS.
        with conn.engine.connect() as other:
            other.exec_driver_sql(f'SET search_path TO "{schema}"')
            other.exec_driver_sql("SET LOCAL lock_timeout='2s'")
            if changed_table == "plan":
                other.execute(update(t.plans).where(t.plans.c.user_id == owner).values(revision=8))
            else:
                other.execute(update(t.plan_proposal_states).where(t.plan_proposal_states.c.proposal_id == identifier)
                              .values(state_revision=1))
            other.commit()

    event.listen(conn, "before_execute", external_writer)
    try:
        with pytest.raises(ApiError) as error:
            service.apply(owner, identifier, domain=operation, target=TARGET)
        assert_api(error, "PREVIEW_STALE", 409)
    finally:
        event.remove(conn, "before_execute", external_writer)
    assert not armed[0]
    conn.commit()
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id == owner)) == (8 if changed_table == "plan" else 7)
    after = ProposalRepository(conn).get(owner, identifier)
    assert after["status"] == after["request_status"] == "ready"
    assert after["accepted_proposal_id"] is None and after["result_response"] is None
    assert after["state_revision"] == (1 if changed_table == "candidate" else 0)
    assert after["changes"] == before["changes"]
