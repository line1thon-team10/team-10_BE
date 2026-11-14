"""Shared preview transactions; domain callbacks never own commit or plan revision."""
import json
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select, update

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.user import User
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.proposals import (
    LegacyProposalUnavailable, LegacyResponseUnavailable, ProposalConflict,
    ProposalNotFound, ProposalRepository, canonical_object,
)


def _json(value: dict | str) -> str:
    return json.dumps(canonical_object(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stale() -> ApiError:
    return ApiError("PREVIEW_STALE", "미리보기의 기준이 달라졌어요. 새 미리보기를 확인해 주세요", 409)


class PlanEffect(Enum):
    """Server domain configuration, never a client-selectable request field."""
    UPDATE = "update_plan"
    CREATE = "create_plan"
    DRAFT = "draft_only"


@dataclass(frozen=True, slots=True, init=False)
class DomainBasis:
    """Domain-owned dependency versions and, without a plan, initial structure."""
    _versions_json: str = field(repr=False)
    structure_hash: str | None

    def __init__(self, dependency_versions: dict | str, *, structure_hash: str | None = None):
        if structure_hash is not None and (not isinstance(structure_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", structure_hash) is None):
            raise ValueError("structure_hash must be a canonical SHA256 hash")
        object.__setattr__(self, "_versions_json", _json(dependency_versions))
        object.__setattr__(self, "structure_hash", structure_hash)

    @property
    def dependency_versions(self) -> dict:
        return canonical_object(self._versions_json)


@dataclass(frozen=True, slots=True)
class PlanState:
    id: str
    revision: int
    created_at: datetime
    approved_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class PreviewContext:
    user: User
    now: datetime
    today: date
    plan: PlanState | None = None


@dataclass(frozen=True, slots=True)
class PreviewDomain:
    """Trusted bounded DB callbacks: inspect reads only; apply writes saved changes.

    Neither callback may commit/rollback, perform external IO/AI or change plan
    headers. The service supplies the current user and one captured UTC time.
    """
    kind: str
    plan_effect: PlanEffect
    inspect_basis: Callable[[Connection, PreviewContext, dict], DomainBasis]
    apply_changes: Callable[[Connection, PreviewContext, dict], dict | str]
    result_schema_version: int = 1
    after_apply: Callable[[Connection, PreviewContext, dict], dict | str] | None = None

    def __post_init__(self):
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("An explicit domain kind is required")
        canonical_object({"kind": self.kind})
        if not isinstance(self.plan_effect, PlanEffect):
            raise ValueError("Plan effect must be a server-owned PlanEffect")
        if not callable(self.inspect_basis) or not callable(self.apply_changes):
            raise ValueError("Explicit domain callbacks are required")
        if self.after_apply is not None and not callable(self.after_apply):
            raise ValueError("after_apply must be a trusted callback")
        if type(self.result_schema_version) is not int or self.result_schema_version < 1:
            raise ValueError("result_schema_version must be a positive integer")


@dataclass(frozen=True, slots=True, repr=False)
class PreviewSnapshot:
    """An immutable preparation basis; JSON accessors always return fresh copies."""
    user_id: str
    kind: str
    plan_effect: PlanEffect
    _target_json: str
    base_date: date
    base_plan_id: str | None
    base_plan_revision: int | None
    basis: DomainBasis

    @property
    def target(self) -> dict:
        return canonical_object(self._target_json)


class ProposalService:
    def __init__(self, connection: Connection, *, clock: Callable[[], datetime] = now_utc):
        self.connection = connection
        self.repository = ProposalRepository(connection)
        self.clock = clock

    @staticmethod
    def _target(target: dict | str) -> str:
        encoded = _json(target)
        if encoded == "{}":
            raise ValueError("An explicit target is required")
        return encoded

    @contextmanager
    def _transaction(self, user_id):
        try:
            try:
                identifier = user_id if isinstance(user_id, UUID) else UUID(user_id)
            except (ValueError, TypeError, AttributeError):
                raise ApiError("UNKNOWN_SESSION", "알 수 없는 익명 ID예요", 401) from None
            # Lock before SAVEPOINT: SQLite must start the real outer transaction.
            user = lock_user(self.connection, identifier)
            if user is None:
                raise ApiError("UNKNOWN_SESSION", "알 수 없는 익명 ID예요", 401)
            now = self.clock()
            if not isinstance(now, datetime) or now.utcoffset() is None:
                raise ValueError("The clock must return a timezone-aware timestamp")
            now = now.astimezone(timezone.utc)
            today = user.demo_date or now.astimezone(ZoneInfo(user.timezone or "Asia/Seoul")).date()
            with self.connection.begin_nested():
                yield PreviewContext(user, now, today)
        except UserBusyError:
            raise ApiError("REQUEST_IN_PROGRESS", "다른 요청이 처리 중이에요. 잠시 후 다시 시도해 주세요", 409) from None
        except ProposalNotFound as error:
            raise ApiError("NO_PREVIEW", "미리보기를 찾지 못했어요", 404 if error.foreign else 400) from None
        except LegacyResponseUnavailable:
            raise ApiError("PREVIEW_STALE", "이미 적용된 과거 이력이지만 최초 응답을 보관하지 않아 복원할 수 없어요. 다시 적용하지 않았어요", 409) from None
        except LegacyProposalUnavailable:
            raise ApiError("PREVIEW_STALE", "과거 미리보기의 기준을 확인할 수 없어요. 새 미리보기를 확인해 주세요", 409) from None
        except ProposalConflict:
            raise _stale() from None

    def _plan(self, user_id: str) -> PlanState | None:
        row = self.connection.execute(select(t.plans).where(t.plans.c.user_id == user_id)).mappings().first()
        if row is None:
            return None
        times = [row[key].replace(tzinfo=timezone.utc) if row[key].tzinfo is None else row[key].astimezone(timezone.utc)
                 for key in ("created_at", "approved_at")]
        return PlanState(row["id"], row["revision"], *times)

    def _snapshot(self, context: PreviewContext, domain: PreviewDomain, target_json: str, *,
                  expected_common: tuple[date, str | None, int | None] | None = None) -> tuple[PreviewContext, PreviewSnapshot]:
        context = replace(context, plan=self._plan(context.user.id))
        common = (context.today, context.plan.id if context.plan else None, context.plan.revision if context.plan else None)
        if expected_common is not None and common != expected_common:
            raise _stale()
        if (domain.plan_effect is PlanEffect.UPDATE) != (context.plan is not None):
            raise _stale()
        basis = domain.inspect_basis(self.connection, context, canonical_object(target_json))
        if not isinstance(basis, DomainBasis):
            raise ValueError("inspect_basis must return DomainBasis")
        if (context.plan is None) != (basis.structure_hash is not None):
            raise ValueError("A structure hash is required only when no approved plan exists")
        if self._plan(context.user.id) != context.plan:
            raise ProposalConflict("A basis reader changed the plan header")
        return context, PreviewSnapshot(context.user.id, domain.kind, domain.plan_effect, target_json, context.today,
            context.plan.id if context.plan else None, context.plan.revision if context.plan else None, basis)

    def capture(self, user_id, *, domain: PreviewDomain, target: dict | str) -> PreviewSnapshot:
        """Capture consistent local bases; finish this caller transaction before AI."""
        target_json = self._target(target)
        with self._transaction(user_id) as context:
            return self._snapshot(context, domain, target_json)[1]

    def create(self, user_id, *, domain: PreviewDomain, target: dict | str, snapshot: PreviewSnapshot,
               normalized_input: dict | str, changes: dict | str, preview_response: dict | str,
               proposal_type: str, description: str, request_id=None, request_text=None,
               preview_id=None, preview_schema_version=1) -> dict:
        """Persist a prepared preview only if its captured basis is still current.

        Preparation/AI happens outside this method and outside its transaction.
        Only candidate/request rows are written; apply_changes is never called.
        """
        if not isinstance(snapshot, PreviewSnapshot):
            raise ValueError("A captured PreviewSnapshot is required")
        target_json = self._target(target)
        identifier = str(uuid4()) if preview_id is None else str(UUID(str(preview_id)))
        normalized_input, changes, response = (canonical_object(value) for value in
                                               (normalized_input, changes, preview_response))
        if "previewId" in response and response["previewId"] != identifier:
            raise ValueError("The preview response must name its actual candidate")
        response["previewId"] = identifier
        with self._transaction(user_id) as context:
            if (snapshot.user_id != context.user.id or snapshot.kind != domain.kind
                    or snapshot._target_json != target_json):
                raise ApiError("NO_PREVIEW", "이 요청의 미리보기가 아니에요", 404)
            _, current = self._snapshot(context, domain, target_json,
                expected_common=(snapshot.base_date, snapshot.base_plan_id, snapshot.base_plan_revision))
            if current != snapshot:
                raise _stale()
            stored = self.repository.create(context.user.id, kind=domain.kind, target=snapshot.target,
                normalized_input=normalized_input, base_date=context.today,
                dependency_versions=snapshot.basis.dependency_versions, changes=changes, preview_response=response,
                now=context.now, proposal_type=proposal_type, description=description,
                base_plan_id=snapshot.base_plan_id, base_plan_revision=snapshot.base_plan_revision,
                base_structure_hash=snapshot.basis.structure_hash, request_id=request_id, request_text=request_text,
                proposal_id=identifier, preview_schema_version=preview_schema_version)
            return canonical_object(stored["preview_response"])

    @staticmethod
    def _preview_id(value) -> str:
        if value is None:
            raise ApiError("PREVIEW_REQUIRED", "적용할 미리보기 ID를 보내 주세요", 409)
        try:
            return str(value if isinstance(value, UUID) else UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise ApiError("NO_PREVIEW", "미리보기 ID가 올바르지 않아요", 400) from None

    def _candidate(self, context: PreviewContext, identifier: str, domain: PreviewDomain, target_json: str) -> dict:
        try:
            state = self.repository.get(context.user.id, identifier)
        except LegacyProposalUnavailable as error:
            if error.known_kind is not None and error.known_kind != domain.kind:
                raise ApiError("NO_PREVIEW", "이 작업의 미리보기가 아니에요", 404) from None
            raise
        if state["kind"] != domain.kind:
            raise ApiError("NO_PREVIEW", "이 작업의 미리보기가 아니에요", 404)
        if state["target"] is not None and _json(state["target"]) != target_json:
            raise ApiError("NO_PREVIEW", "이 대상의 미리보기가 아니에요", 404)
        # Legacy targets can be unknown. That is never permission to execute,
        # and a known past success must retain its more specific explanation.
        if state["legacy"]:
            if state["status"] == "applied":
                raise LegacyResponseUnavailable()
            raise LegacyProposalUnavailable()
        if state["target"] is None:
            raise ProposalConflict("The candidate has no verifiable target")
        return state

    @staticmethod
    def _require_fresh(state: dict, current: PreviewSnapshot) -> None:
        if (state["base_date"], state["base_plan_id"], state["base_plan_revision"], state["base_structure_hash"]) != (
                current.base_date, current.base_plan_id, current.base_plan_revision, current.basis.structure_hash):
            raise _stale()
        if _json(state["dependency_versions"]) != current.basis._versions_json:
            raise _stale()

    def apply(self, user_id, preview_id, *, domain: PreviewDomain, target: dict | str) -> dict:
        """Apply only saved changes and commit no transaction on behalf of callers."""
        identifier, target_json = self._preview_id(preview_id), self._target(target)
        with self._transaction(user_id) as context:
            state = self._candidate(context, identifier, domain, target_json)
            if state["status"] == "applied":
                if state["request_status"] != "accepted" or state["accepted_proposal_id"] != identifier:
                    raise ProposalConflict("The applied candidate has no matching accepted request")
                # Replay precedes live plan/date/dependency checks, including
                # plan creation/deletion or domain callbacks that no longer work.
                return canonical_object(state["result_response"])
            if (state["status"] != "ready" or state["request_status"] != "ready"
                    or state["accepted_proposal_id"] is not None):
                raise _stale()
            context, current = self._snapshot(context, domain, target_json,
                expected_common=(state["base_date"], state["base_plan_id"], state["base_plan_revision"]))
            self._require_fresh(state, current)

            if domain.plan_effect is PlanEffect.CREATE:
                initial = PlanState(str(uuid4()), 1, context.now, context.now)
                self.connection.execute(t.plans.insert().values(id=initial.id, user_id=context.user.id,
                    revision=1, created_at=initial.created_at, approved_at=initial.approved_at))
                context = replace(context, plan=initial)
            response = canonical_object(domain.apply_changes(self.connection, context, canonical_object(state["changes"])))
            if self._plan(context.user.id) != context.plan:
                raise ProposalConflict("The domain callback changed a service-owned plan header")

            if domain.plan_effect is PlanEffect.UPDATE:
                changed = self.connection.execute(update(t.plans).where(
                    t.plans.c.id == context.plan.id, t.plans.c.user_id == context.user.id,
                    t.plans.c.revision == context.plan.revision).values(revision=context.plan.revision + 1))
                expected_plan = replace(context.plan, revision=context.plan.revision + 1)
                if changed.rowcount != 1 or self._plan(context.user.id) != expected_plan:
                    raise ProposalConflict("The plan revision compare-and-set failed")
                context = replace(context, plan=expected_plan)
            if domain.after_apply is not None:
                response = canonical_object(domain.after_apply(self.connection, context, response))
                if self._plan(context.user.id) != context.plan:
                    raise ProposalConflict("The after-apply callback changed a service-owned plan header")
            applied = self.repository.mark_applied(context.user.id, identifier,
                expected_revision=state["state_revision"], response=response, now=context.now,
                schema_version=domain.result_schema_version)
            if not applied.changed:
                # Normal concurrent retries replay before executing the domain.
                # A late state change must not commit the domain a second time.
                raise ProposalConflict("The candidate changed after its domain callback")
            return canonical_object(applied.proposal["result_response"])

    def cancel(self, user_id, preview_id, *, domain: PreviewDomain, target: dict | str) -> dict:
        """Cancel only this candidate; retries preserve its first decided_at.

        Ready candidates may be cancelled after their basis/date has changed.
        The request, siblings and approved domain data are never changed here.
        """
        identifier, target_json = self._preview_id(preview_id), self._target(target)
        with self._transaction(user_id) as context:
            state = self._candidate(context, identifier, domain, target_json)
            if state["status"] == "cancelled":
                return {"cancelled": True}
            if state["status"] != "ready":
                raise _stale()
            self.repository.cancel(context.user.id, identifier, expected_revision=state["state_revision"], now=context.now)
            return {"cancelled": True}
