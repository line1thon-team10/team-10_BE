"""Common preview service uses caller transactions and domain-provided bases."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.proposals import InvalidProposalJSON, ProposalRepository, canonical_hash
from app.services.proposals import DomainBasis, PlanEffect, PreviewDomain, ProposalService
from tests.test_proposal_storage import DAY, NOW, owner

STRUCTURE = canonical_hash({"materials": [], "goals": [], "schedules": []})
TARGET = {"type": "profile", "section": "goal"}


def domain(*, effect=PlanEffect.DRAFT, versions=None, inspect=None, apply=None, kind="goal"):
    versions = {"schedule": 1, "materials": []} if versions is None else versions

    def basis(conn, context, target):
        return DomainBasis(versions, structure_hash=STRUCTURE if context.plan is None else None)

    def execute(conn, context, changes):
        conn.execute(update(t.users).where(t.users.c.id == context.user.id).values(onboarding_status=changes["status"]))
        return {"applied": True, "at": context.now.isoformat(), "planId": context.plan.id if context.plan else None}

    return PreviewDomain(kind, effect, inspect or basis, apply or execute)


def create(service, user, operation=None, **overrides):
    operation = operation or domain()
    target = overrides.pop("target", TARGET)
    snapshot = overrides.pop("snapshot", None) or service.capture(user, domain=operation, target=target)
    return service.create(user, domain=operation, target=target, snapshot=snapshot, **{
        "normalized_input": {"text": "  원문  "}, "changes": {"status": "completed"},
        "preview_response": {"summary": {"pages": 0}}, "proposal_type": "change", "description": "검증된 후보",
        **overrides,
    })


def plan(conn, user, *, revision=7):
    identifier = str(uuid4())
    conn.execute(t.plans.insert().values(id=identifier, user_id=user, revision=revision, created_at=NOW, approved_at=NOW))
    return identifier


def assert_api(error, code, status):
    assert error.value.code == code
    assert error.value.status == status


@pytest.mark.parametrize("effect", list(PlanEffect))
def test_capture_and_create_never_approve_or_mutate_existing_domain(domain_connection, owner, effect):
    conn = domain_connection
    if effect is PlanEffect.UPDATE:
        plan(conn, owner)
    before = [dict(row) for row in conn.execute(select(t.plans)).mappings()]
    called = []
    operation = domain(effect=effect, apply=lambda *_: called.append(True))
    service = ProposalService(conn, clock=lambda: NOW)
    result = create(service, owner, operation)
    assert result == {"previewId": result["previewId"], "summary": {"pages": 0}}
    assert not called
    assert [dict(row) for row in conn.execute(select(t.plans)).mappings()] == before
    assert conn.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == owner)) == "draft"
    stored = ProposalRepository(conn).get(owner, result["previewId"])
    assert stored["status"] == stored["request_status"] == "ready"
    assert stored["normalized_input"] == {"text": "  원문  "} and stored["changes"] == {"status": "completed"}
    assert stored["base_date"] == DAY
    assert stored["base_plan_revision"] == (7 if effect is PlanEffect.UPDATE else None)
    assert stored["base_structure_hash"] == (None if effect is PlanEffect.UPDATE else STRUCTURE)


def test_preparation_snapshot_and_first_preview_response_have_no_mutable_aliases(domain_connection, owner):
    conn = domain_connection
    versions = {"materials": [{"id": "first", "revision": 1.0}], "enabled": True}
    target = {"type": "profile", "sections": ["goal"]}
    operation = domain(versions=versions)
    service = ProposalService(conn, clock=lambda: NOW)
    snapshot = service.capture(owner, domain=operation, target=target)
    assert snapshot.basis.dependency_versions["materials"][0]["revision"] == 1
    target["sections"].append("other")
    snapshot.target["sections"].append("other")
    snapshot.basis.dependency_versions["materials"].clear()
    assert snapshot.target == {"type": "profile", "sections": ["goal"]}
    assert snapshot.basis.dependency_versions["materials"] == [{"id": "first", "revision": 1}]
    with pytest.raises(FrozenInstanceError):
        snapshot.base_date = date(2026, 11, 15)
    result = create(service, owner, operation, snapshot=snapshot, target=snapshot.target)
    result["summary"]["pages"] = 100
    assert ProposalRepository(conn).get(owner, result["previewId"])["preview_response"]["summary"]["pages"] == 0


@pytest.mark.parametrize("changed", ["date", "dependency", "structure", "plan_revision", "plan_created", "target", "kind", "owner"])
def test_create_rejects_changed_preparation_without_any_candidate_rows(domain_connection, owner, changed):
    conn = domain_connection
    effect = PlanEffect.UPDATE if changed == "plan_revision" else PlanEffect.DRAFT
    if effect is PlanEffect.UPDATE:
        plan(conn, owner)
    versions = {"material": 1}
    operation = domain(effect=effect, versions=versions)
    service = ProposalService(conn, clock=lambda: NOW)
    snapshot = service.capture(owner, domain=operation, target=TARGET)
    target, applying_user = TARGET, owner
    if changed == "date":
        service.clock = lambda: NOW + timedelta(days=1)
    elif changed == "dependency":
        versions["material"] = 2
    elif changed == "structure":
        operation = replace(operation, inspect_basis=lambda *_: DomainBasis(versions, structure_hash="b" * 64))
    elif changed == "plan_revision":
        conn.execute(update(t.plans).where(t.plans.c.user_id == owner).values(revision=8))
    elif changed == "plan_created":
        plan(conn, owner)
    elif changed == "target":
        target = {"type": "profile", "section": "schedule"}
    elif changed == "kind":
        operation = replace(operation, kind="schedule")
    else:
        applying_user = str(uuid4())
        conn.execute(t.users.insert().values(id=applying_user, created_at=NOW, updated_at=NOW))
    with pytest.raises(ApiError) as error:
        create(service, applying_user, operation, snapshot=snapshot, target=target)
    mismatch = changed in ("target", "kind", "owner")
    assert_api(error, "NO_PREVIEW" if mismatch else "PREVIEW_STALE", 404 if mismatch else 409)
    for table in (t.plan_change_requests, t.plan_proposals, t.plan_proposal_states):
        assert conn.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("zone,instant,expected_day", [
    ("Asia/Seoul", datetime(2026, 11, 14, 14, 59, 59, tzinfo=timezone.utc), date(2026, 11, 14)),
    ("Asia/Seoul", datetime(2026, 11, 14, 15, tzinfo=timezone.utc), date(2026, 11, 15)),
    ("America/Los_Angeles", datetime(2026, 11, 14, 7, 59, 59, tzinfo=timezone.utc), date(2026, 11, 13)),
    ("America/Los_Angeles", datetime(2026, 11, 14, 8, tzinfo=timezone.utc), date(2026, 11, 14)),
])
def test_captured_time_uses_latest_timezone_once(domain_connection, owner, zone, instant, expected_day):
    conn = domain_connection
    conn.execute(update(t.users).where(t.users.c.id == owner).values(timezone=zone))
    ticks, seen = [], []

    def clock():
        ticks.append(instant)
        return instant

    def inspect_basis(connection, context, target):
        seen.append(context)
        return DomainBasis({}, structure_hash=STRUCTURE)

    service = ProposalService(conn, clock=clock)
    snapshot = service.capture(owner, domain=domain(inspect=inspect_basis), target=TARGET)
    assert ticks == [instant] and snapshot.base_date == expected_day
    assert seen[0].now == instant and seen[0].user.timezone == zone


def test_demo_date_is_refetched_after_lock_and_overrides_server_calendar(domain_connection, owner):
    conn = domain_connection
    armed = [True]

    def change_before_user_refetch(connection, clause, *_):
        if armed[0] and getattr(clause, "is_select", False) and "users.timezone" in str(clause):
            armed[0] = False
            connection.execute(update(t.users).where(t.users.c.id == owner).values(demo_date=date(2026, 2, 3)))

    event.listen(conn, "before_execute", change_before_user_refetch)
    try:
        snapshot = ProposalService(conn, clock=lambda: NOW).capture(owner, domain=domain(), target=TARGET)
    finally:
        event.remove(conn, "before_execute", change_before_user_refetch)
    assert not armed[0] and snapshot.base_date == date(2026, 2, 3)


def test_create_rolls_back_partial_repository_insert_and_caller_can_still_rollback(domain_connection, owner, monkeypatch):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    snapshot = service.capture(owner, domain=domain(), target=TARGET)
    conn.commit()
    original_get = service.repository.get

    def fail_after_insert(*args):
        original_get(*args)
        raise RuntimeError("failed after request/proposal/state insertion")

    monkeypatch.setattr(service.repository, "get", fail_after_insert)
    with pytest.raises(RuntimeError, match="after request"):
        create(service, owner, snapshot=snapshot)
    conn.commit()  # Even a caller that catches this failure cannot persist partial service writes.
    assert conn.scalar(select(func.count()).select_from(t.plan_proposals)) == 0
    assert conn.scalar(select(func.count()).select_from(t.plan_change_requests)) == 0
    assert conn.scalar(select(func.count()).select_from(t.plan_proposal_states)) == 0
    monkeypatch.setattr(service.repository, "get", original_get)
    created = create(service, owner, snapshot=snapshot)
    conn.rollback()
    assert conn.scalar(select(t.plan_proposals.c.id).where(t.plan_proposals.c.id == created["previewId"])) is None


@pytest.mark.parametrize("invalid", ['{"same":1,"same":2}', {"x": "\x00"}, {"x": float("nan")}])
def test_create_invalid_prepared_json_never_reaches_sql(domain_connection, owner, invalid):
    conn = domain_connection
    service = ProposalService(conn, clock=lambda: NOW)
    snapshot = service.capture(owner, domain=domain(), target=TARGET)

    def unexpected_sql(*_):
        pytest.fail("invalid prepared JSON reached SQL")

    event.listen(conn, "before_cursor_execute", unexpected_sql)
    try:
        with pytest.raises(InvalidProposalJSON):
            create(service, owner, snapshot=snapshot, changes=invalid)
    finally:
        event.remove(conn, "before_cursor_execute", unexpected_sql)


@pytest.mark.parametrize("operation_name", ["create", "apply"])
@pytest.mark.parametrize("changed", ["date", "revision"])
def test_stale_common_basis_is_rejected_before_domain_inspection(domain_connection, owner, operation_name, changed):
    conn = domain_connection
    operation = domain(effect=PlanEffect.UPDATE if changed == "revision" else PlanEffect.DRAFT)
    if changed == "revision":
        plan(conn, owner)
    service = ProposalService(conn, clock=lambda: NOW)
    snapshot = service.capture(owner, domain=operation, target=TARGET)
    identifier = create(service, owner, operation, snapshot=snapshot)["previewId"] if operation_name == "apply" else None
    if changed == "date":
        conn.execute(update(t.users).where(t.users.c.id == owner).values(demo_date=DAY + timedelta(days=1)))
    else:
        conn.execute(update(t.plans).where(t.plans.c.user_id == owner).values(revision=8))

    def forbidden(*_):
        pytest.fail("an already stale common basis must not execute domain inspection")

    operation = replace(operation, inspect_basis=forbidden)
    with pytest.raises(ApiError) as error:
        if operation_name == "create":
            create(service, owner, operation, snapshot=snapshot)
        else:
            service.apply(owner, identifier, domain=operation, target=TARGET)
    assert_api(error, "PREVIEW_STALE", 409)
