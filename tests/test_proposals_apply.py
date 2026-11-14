from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Update

from app.core.responses import ApiError
from app.db import schema as t
from app.db.migrations import migration_config
from app.repositories.proposals import InvalidProposalJSON, ProposalRepository, ProposalTransition
from app.services.proposals import DomainBasis, PlanEffect, ProposalService
from tests.test_proposal_storage import DAY, NOW, owner
from tests.test_proposal_storage_migration import legacy_history
from tests.test_proposals import STRUCTURE, TARGET, assert_api, create, domain, plan


@pytest.mark.parametrize("effect", list(PlanEffect))
def test_apply_owns_revision_and_replays_first_response_before_live_freshness(domain_connection, owner, effect):
    conn = domain_connection
    if effect is PlanEffect.UPDATE:
        plan(conn, owner, revision=7)
    calls, context_seen = [], []

    def execute(connection, context, changes):
        calls.append(changes)
        context_seen.append(context)
        connection.execute(update(t.users).where(t.users.c.id == owner).values(onboarding_status=changes["status"]))
        return {"applied": True, "nested": {"saved": changes["status"]}, "at": context.now.isoformat()}

    operation = domain(effect=effect, apply=execute)
    service = ProposalService(conn, clock=lambda: NOW)
    candidate = create(service, owner, operation)
    conn.commit()
    identifier = candidate["previewId"]
    result = service.apply(owner, identifier, domain=operation, target=TARGET)
    first = ProposalRepository(conn).get(owner, identifier)
    assert result == first["result_response"]
    assert first["result_captured_at"] == first["decided_at"] == NOW
    assert first["status"] == "applied" and first["request_status"] == "accepted"
    assert first["accepted_proposal_id"] == identifier and first["state_revision"] == 1
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "completed"
    stored_plan = conn.execute(select(t.plans).where(t.plans.c.user_id == owner)).mappings().first()
    if effect is PlanEffect.DRAFT:
        assert stored_plan is None and context_seen[0].plan is None
    else:
        assert stored_plan["revision"] == (8 if effect is PlanEffect.UPDATE else 1)
        assert context_seen[0].plan.revision == (7 if effect is PlanEffect.UPDATE else 1)
    conn.commit()

    # All present bases can change after success; callbacks must not run again.
    if stored_plan:
        conn.execute(update(t.plans).where(t.plans.c.user_id == owner).values(revision=50))
    else:
        plan(conn, owner)
    conn.execute(update(t.users).where(t.users.c.id == owner).values(demo_date=DAY + timedelta(days=20)))
    conn.commit()
    service.clock = lambda: NOW + timedelta(days=20)

    def forbidden(*_):
        pytest.fail("an applied preview performed freshness or application work")

    replay_domain = replace(operation, inspect_basis=forbidden, apply_changes=forbidden)
    result["nested"]["saved"] = "caller mutation"
    replay = service.apply(owner, identifier.upper().replace("-", ""), domain=replay_domain, target=TARGET)
    assert replay == first["result_response"]
    replay["nested"]["saved"] = "second caller mutation"
    assert ProposalRepository(conn).get(owner, identifier) == first
    assert calls == [{"status": "completed"}]


@pytest.mark.parametrize("bad_id,status,code", [
    (None, 409, "PREVIEW_REQUIRED"), ("", 400, "NO_PREVIEW"), ("missing", 400, "NO_PREVIEW"),
    (1, 400, "NO_PREVIEW"), (True, 400, "NO_PREVIEW"), ({}, 400, "NO_PREVIEW"),
])
def test_apply_invalid_ids_are_rejected_before_any_database_access(domain_connection, owner, bad_id, status, code):
    conn = domain_connection

    def forbidden(*_):
        pytest.fail("invalid candidate identifier reached SQL")

    event.listen(conn, "before_cursor_execute", forbidden)
    try:
        with pytest.raises(ApiError) as error:
            ProposalService(conn).apply(owner, bad_id, domain=domain(), target=TARGET)
        assert_api(error, code, status)
    finally:
        event.remove(conn, "before_cursor_execute", forbidden)


@pytest.mark.parametrize("case", ["missing", "deleted", "foreign", "kind", "target", "boolean_target"])
def test_apply_checks_owner_kind_target_and_deleted_ids_before_callbacks(domain_connection, owner, case):
    conn = domain_connection
    service, operation = ProposalService(conn, clock=lambda: NOW), domain()
    target = {"selected": True} if case == "boolean_target" else TARGET
    identifier = create(service, owner, operation, target=target)["previewId"]
    user = owner
    if case == "missing":
        identifier = str(uuid4())
    elif case == "deleted":
        conn.execute(delete(t.plan_proposals).where(t.plan_proposals.c.id == identifier))
    elif case == "foreign":
        user = str(uuid4())
        conn.execute(t.users.insert().values(id=user, created_at=NOW, updated_at=NOW))
    elif case == "kind":
        operation = replace(operation, kind="different")
    elif case == "boolean_target":
        target = {"selected": 1}
    else:
        target = {"type": "profile", "section": "another"}

    def forbidden(*_):
        pytest.fail("mismatched candidate reached a domain callback")

    operation = replace(operation, inspect_basis=forbidden, apply_changes=forbidden)
    with pytest.raises(ApiError) as error:
        service.apply(user, identifier, domain=operation, target=target)
    assert_api(error, "NO_PREVIEW", 400 if case in ("missing", "deleted") else 404)


@pytest.mark.parametrize("changed", ["date", "structure", "plan_id", "plan_revision", "plan_created",
                                   "material", "schedule", "records", "policy", "boolean_version"])
def test_apply_rejects_changed_bases_before_any_domain_write(domain_connection, owner, changed):
    conn = domain_connection
    planned = changed in ("plan_id", "plan_revision")
    if planned:
        plan(conn, owner)
    versions = {"material": 1, "schedule": 2, "records": [{"id": "r", "rev": 3}], "policy": "v1", "flag": True}
    operation = domain(effect=PlanEffect.UPDATE if planned else PlanEffect.DRAFT, versions=versions)
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner, operation)["previewId"]
    if changed == "date":
        conn.execute(update(t.users).where(t.users.c.id == owner).values(demo_date=DAY + timedelta(days=1)))
    elif changed == "structure":
        operation = replace(operation, inspect_basis=lambda *_: DomainBasis(versions, structure_hash="b" * 64))
    elif changed == "plan_id":
        # Same revision, a different current plan: preserve the old referenced plan under another owner.
        other = str(uuid4())
        conn.execute(t.users.insert().values(id=other, created_at=NOW, updated_at=NOW))
        conn.execute(update(t.plans).where(t.plans.c.user_id == owner).values(user_id=other))
        plan(conn, owner)
    elif changed == "plan_revision":
        conn.execute(update(t.plans).where(t.plans.c.user_id == owner).values(revision=8))
    elif changed == "plan_created":
        plan(conn, owner)
    elif changed == "boolean_version":
        versions["flag"] = 1
    else:
        versions[changed] = {"changed": True}
    conn.commit()
    original = ProposalRepository(conn).get(owner, identifier)
    calls = []
    operation = replace(operation, apply_changes=lambda *_: calls.append(True))
    with pytest.raises(ApiError) as error:
        service.apply(owner, identifier, domain=operation, target=TARGET)
    assert_api(error, "PREVIEW_STALE", 409)
    assert not calls and ProposalRepository(conn).get(owner, identifier) == original
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"


def test_old_ready_candidate_is_valid_without_arbitrary_expiry(domain_connection, owner):
    conn = domain_connection
    conn.execute(update(t.users).where(t.users.c.id == owner).values(demo_date=DAY))
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner)["previewId"]
    conn.commit()
    service.clock = lambda: NOW + timedelta(days=90)
    result = service.apply(owner, identifier, domain=domain(), target=TARGET)
    assert result["applied"] is True
    assert result["at"] == (NOW + timedelta(days=90)).isoformat()


def test_apply_uses_saved_changes_only_and_protects_input_and_output_aliases(domain_connection, owner):
    conn = domain_connection
    supplied = {"status": "completed", "nested": {"pages": [1, 2]}}
    service = ProposalService(conn, clock=lambda: NOW)
    result_alias, changes_alias = [], []

    def execute(connection, context, changes):
        changes_alias.append(changes)
        assert changes == {"status": "completed", "nested": {"pages": [1, 2]}}
        changes["nested"]["pages"].append(3)
        response = {"saved": changes["nested"]}
        result_alias.append(response)
        return response

    operation = domain(apply=execute)
    identifier = create(service, owner, operation, changes=supplied)["previewId"]
    supplied["nested"]["pages"].append(999)
    result = service.apply(owner, identifier, domain=operation, target=TARGET)
    changes_alias[0]["nested"]["pages"].append(4)
    result_alias[0]["saved"]["pages"].append(5)
    assert result == {"saved": {"pages": [1, 2, 3]}}
    stored = ProposalRepository(conn).get(owner, identifier)
    assert stored["changes"] == {"status": "completed", "nested": {"pages": [1, 2]}}
    assert stored["result_response"] == result


@pytest.mark.parametrize("effect,failure", [(effect, failure) for effect in PlanEffect
    for failure in ("domain", "json", "sql", "state_cas", "plan_cas", "late_replay")
    if failure != "plan_cas" or effect is PlanEffect.UPDATE])
def test_apply_failure_rolls_back_domain_plan_request_and_candidate(domain_connection, owner, effect, failure, monkeypatch):
    conn = domain_connection
    if effect is PlanEffect.UPDATE:
        plan(conn, owner)
    service = ProposalService(conn, clock=lambda: NOW)

    def execute(connection, context, changes):
        connection.execute(update(t.users).where(t.users.c.id == owner).values(onboarding_status="completed"))
        if failure == "domain":
            raise RuntimeError("domain failed after a write")
        if failure == "sql":
            connection.execute(t.users.insert().values(id=owner, created_at=NOW, updated_at=NOW))
        return {"bad": float("nan")} if failure == "json" else {"applied": True}

    operation = domain(effect=effect, apply=execute)
    identifier = create(service, owner, operation)["previewId"]
    conn.commit()
    before = ProposalRepository(conn).get(owner, identifier)
    plan_before = [dict(row) for row in conn.execute(select(t.plans)).mappings()]
    conn.commit()
    armed = [failure in ("state_cas", "plan_cas")]

    def lose_cas(connection, clause, *_):
        table = t.plans if failure == "plan_cas" else t.plan_proposal_states
        if armed[0] and isinstance(clause, Update) and clause.table is table:
            armed[0] = False
            if table is t.plans:
                connection.execute(update(table).where(table.c.user_id == owner).values(revision=99))
            else:
                connection.execute(update(table).where(table.c.proposal_id == identifier).values(state_revision=99))

    event.listen(conn, "before_execute", lose_cas)
    if failure == "late_replay":
        monkeypatch.setattr(service.repository, "mark_applied", lambda *args, **kwargs: ProposalTransition(before, False))
    expected = {"domain": RuntimeError, "json": InvalidProposalJSON, "sql": IntegrityError}.get(failure, ApiError)
    try:
        with pytest.raises(expected) as error:
            service.apply(owner, identifier, domain=operation, target=TARGET)
        if expected is ApiError:
            assert_api(error, "PREVIEW_STALE", 409)
    finally:
        event.remove(conn, "before_execute", lose_cas)
    assert not armed[0]
    conn.commit()  # Catching an error must still never persist a partial service operation.
    assert ProposalRepository(conn).get(owner, identifier) == before
    assert [dict(row) for row in conn.execute(select(t.plans)).mappings()] == plan_before
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"


@pytest.mark.parametrize("effect,field", [(effect, field) for effect in PlanEffect
    for field in ("revision", "identity", "timestamp") if effect is not PlanEffect.DRAFT or field == "identity"])
def test_callbacks_cannot_take_ownership_of_plan_headers(domain_connection, owner, effect, field):
    conn = domain_connection
    if effect is PlanEffect.UPDATE:
        plan(conn, owner)
    service = ProposalService(conn, clock=lambda: NOW)

    def execute(connection, context, changes):
        connection.execute(update(t.users).where(t.users.c.id == owner).values(onboarding_status="completed"))
        if effect is PlanEffect.DRAFT:
            plan(connection, owner)
        elif field == "revision":
            connection.execute(update(t.plans).where(t.plans.c.user_id == owner).values(revision=99))
        elif field == "identity":
            other = str(uuid4())
            connection.execute(t.users.insert().values(id=other, created_at=NOW, updated_at=NOW))
            connection.execute(update(t.plans).where(t.plans.c.user_id == owner).values(user_id=other))
        else:
            connection.execute(update(t.plans).where(t.plans.c.user_id == owner).values(approved_at=NOW + timedelta(seconds=1)))
        return {"applied": True}

    operation = domain(effect=effect, apply=execute)
    identifier = create(service, owner, operation)["previewId"]
    conn.commit()
    before = [dict(row) for row in conn.execute(select(t.plans)).mappings()]
    with pytest.raises(ApiError) as error:
        service.apply(owner, identifier, domain=operation, target=TARGET)
    assert_api(error, "PREVIEW_STALE", 409)
    conn.commit()
    assert [dict(row) for row in conn.execute(select(t.plans)).mappings()] == before
    assert ProposalRepository(conn).get(owner, identifier)["status"] == "ready"
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"
    assert conn.scalar(select(func.count()).select_from(t.users)) == 1


def test_success_is_still_owned_by_the_caller_transaction(domain_connection, owner):
    conn = domain_connection
    operation = domain(effect=PlanEffect.CREATE)
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner, operation)["previewId"]
    conn.commit()
    service.apply(owner, identifier, domain=operation, target=TARGET)
    conn.rollback()
    assert conn.scalar(select(func.count()).select_from(t.plans)) == 0
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"
    assert ProposalRepository(conn).get(owner, identifier)["status"] == "ready"


@pytest.mark.parametrize("terminal", ["cancelled", "stale"])
def test_cancelled_or_stale_candidates_never_execute(domain_connection, owner, terminal):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    identifier = create(service, owner)["previewId"]
    method = service.repository.cancel if terminal == "cancelled" else service.repository.mark_stale
    method(owner, identifier, expected_revision=0, now=NOW)
    before = service.repository.get(owner, identifier)

    def forbidden(*_):
        pytest.fail("terminal candidates may not execute callbacks")

    with pytest.raises(ApiError) as error:
        service.apply(owner, identifier, domain=domain(inspect=forbidden, apply=forbidden), target=TARGET)
    assert_api(error, "PREVIEW_STALE", 409)
    assert service.repository.get(owner, identifier) == before


def test_legacy_applied_is_distinct_and_never_reconstructed_or_executed(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0006_quiz_answer_storage")
    user, plan_id, identifiers = legacy_history(conn)
    command.upgrade(config, "head")

    def forbidden(*_):
        pytest.fail("legacy candidates may not inspect present bases or execute changes")

    operation = domain(effect=PlanEffect.UPDATE, kind="redistribute", inspect=forbidden, apply=forbidden)
    service = ProposalService(conn, clock=lambda: NOW)
    for index in (0, 1, 2, 3, 4):
        identifier = identifiers[index]
        before = service.repository.get(user, identifier)
        with pytest.raises(ApiError) as error:
            service.apply(user, identifier, domain=operation, target={"type": "plan", "id": plan_id})
        assert_api(error, "PREVIEW_STALE", 409)
        if index == 0:
            assert "이미 적용된" in error.value.message and "복원할 수 없어요" in error.value.message
        else:
            assert "과거 미리보기" in error.value.message and "이미 적용된" not in error.value.message
        assert service.repository.get(user, identifier) == before


@pytest.mark.parametrize("history", ["ready", "selected", "unselected_sibling"])
def test_owned_old_writer_candidate_without_state_preserves_known_application(domain_connection, owner, history):
    conn = domain_connection
    request_id, identifier = str(uuid4()), str(uuid4())
    conn.execute(t.plan_change_requests.insert().values(id=request_id, user_id=owner, kind="goal", status="ready",
        input_payload={}, created_at=NOW, updated_at=NOW))
    conn.execute(t.plan_proposals.insert().values(id=identifier, request_id=request_id, proposal_type="change",
        description="old writer", changes={"status": "completed"}, created_at=NOW))
    selected = identifier
    if history == "unselected_sibling":
        selected = str(uuid4())
        conn.execute(t.plan_proposals.insert().values(id=selected, request_id=request_id, proposal_type="change",
            description="selected sibling", changes={"status": "completed"}, created_at=NOW))
    if history != "ready":
        conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == request_id).values(
            status="accepted", accepted_proposal_id=selected, decided_at=NOW))
    conn.commit()
    service = ProposalService(conn, clock=lambda: NOW)
    with pytest.raises(ApiError) as error:
        service.apply(owner, identifier, domain=domain(), target=TARGET)
    assert_api(error, "PREVIEW_STALE", 409)
    if history == "selected":
        assert "이미 적용된" in error.value.message and "복원할 수 없어요" in error.value.message
    else:
        assert "과거 미리보기" in error.value.message
    assert conn.scalar(select(func.count()).select_from(t.plan_proposal_states)) == 0
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"
    for method in (service.apply, service.cancel):
        with pytest.raises(ApiError) as mismatch:
            method(owner, identifier, domain=domain(kind="different"), target=TARGET)
        assert_api(mismatch, "NO_PREVIEW", 404)
