"""Durable preview primitives; callers own the surrounding transaction."""
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Connection, null, select

from app.db import schema as t
from app.repositories.locking import lock_user


class InvalidProposalJSON(ValueError):
    """The input cannot be stored losslessly in both SQLite and PostgreSQL."""


def _json_string(value: str) -> str:
    if "\x00" in value:
        raise InvalidProposalJSON("JSON strings and keys must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidProposalJSON("JSON strings and keys must be valid Unicode") from None
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidProposalJSON("Duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise InvalidProposalJSON("JSON numbers must be finite")


def canonical_object(value: dict | str) -> dict:
    """Validate/copy an object, or parse raw JSON before duplicate keys are lost.

    Whitespace within strings and array ordering are preserved. Domain-specific
    normalization (for example sorting a set of dependency IDs) is the caller's
    responsibility, not an implicit change to the recorded input.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        except (json.JSONDecodeError, RecursionError):
            raise InvalidProposalJSON("A valid JSON object is required") from None
    if not isinstance(value, dict):
        raise InvalidProposalJSON("A JSON object is required")
    active = set()

    def visit(item):
        if item is None or type(item) in (bool, int):
            return item
        if isinstance(item, str):
            return _json_string(item)
        if type(item) is float:
            if not math.isfinite(item):
                raise InvalidProposalJSON("JSON numbers must be finite")
            return item
        if isinstance(item, (dict, list)):
            if id(item) in active:
                raise InvalidProposalJSON("Circular JSON objects are not allowed")
            active.add(id(item))
            try:
                if isinstance(item, list):
                    return [visit(child) for child in item]
                result = {}
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise InvalidProposalJSON("JSON object keys must be strings")
                    result[_json_string(key)] = visit(child)
                return result
            finally:
                active.remove(id(item))
        raise InvalidProposalJSON("Only JSON object, array, string, boolean, finite number or null values are allowed")

    try:
        result = visit(value)
        # Also reject oversized integers/depth unsupported by the JSON encoder.
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result
    except (RecursionError, OverflowError):
        raise InvalidProposalJSON("JSON nesting or numbers exceed the supported representation") from None


def canonical_hash(value: dict | str) -> str:
    encoded = json.dumps(canonical_object(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProposalNotFound(LookupError):
    def __init__(self, *, foreign=False):
        super().__init__("The proposal or its owning request was not found")
        self.foreign = foreign


class ProposalConflict(RuntimeError):
    """Stored ownership, basis or state no longer matches the expected value."""


class LegacyProposalUnavailable(ProposalConflict):
    """A legacy candidate has no complete basis and cannot be executed."""


class LegacyResponseUnavailable(LegacyProposalUnavailable):
    """Application is known, but its original response was never stored."""


def _uuid(value) -> str:
    try:
        return str(value if isinstance(value, UUID) else UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A UUID identifier is required") from None


def _time(value) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("A canonical lowercase SHA256 hash is required")
    return value


def _text(value, label, *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{label} must be a string" + (" with content" if nonempty else ""))
    return _json_string(value)


def _row(row):
    return {key: value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value
            for key, value in dict(row).items()}


class ProposalRepository:
    def __init__(self, connection: Connection):
        self.connection = connection

    def _lock(self, user_id):
        if lock_user(self.connection, UUID(user_id)) is None:
            raise ProposalNotFound()

    def _request(self, user_id, request_id):
        row = self.connection.execute(select(t.plan_change_requests).where(
            t.plan_change_requests.c.id == request_id)).mappings().first()
        if row is None or row["user_id"] != user_id:
            raise ProposalNotFound(foreign=row is not None)
        return _row(row)

    def get(self, user_id, proposal_id) -> dict:
        user_id, proposal_id = _uuid(user_id), _uuid(proposal_id)
        proposal = self.connection.execute(select(t.plan_proposals).where(
            t.plan_proposals.c.id == proposal_id)).mappings().first()
        if proposal is None:
            raise ProposalNotFound()
        request = self._request(user_id, proposal["request_id"])
        state = self.connection.execute(select(t.plan_proposal_states).where(
            t.plan_proposal_states.c.proposal_id == proposal_id)).mappings().first()
        if state is None:
            raise LegacyProposalUnavailable("The candidate predates complete preview metadata")
        if state["user_id"] != user_id or state["kind"] != request["kind"]:
            raise ProposalConflict("Candidate owner or kind does not match its request")
        if not state["legacy"] and (state["base_plan_id"], state["base_plan_revision"]) != (
                request["base_plan_id"], request["base_plan_revision"]):
            raise ProposalConflict("Candidate basis does not match its request")
        return {**_row(state), "request_id": request["id"], "request_status": request["status"],
                "accepted_proposal_id": request["accepted_proposal_id"],
                "proposal_type": proposal["proposal_type"], "description": proposal["description"],
                "changes": proposal["changes"], "created_at": _row(proposal)["created_at"]}

    def ready(self, user_id, *, kind: str, input_hash: str | None = None) -> list[dict]:
        """Find stored ready candidates; date/version freshness belongs to SYS-05."""
        user_id = _uuid(user_id)
        _text(kind, "kind", nonempty=True)
        query = select(t.plan_proposal_states.c.proposal_id).join(
            t.plan_proposals, t.plan_proposals.c.id == t.plan_proposal_states.c.proposal_id).where(
            t.plan_proposal_states.c.user_id == user_id, t.plan_proposal_states.c.kind == kind,
            t.plan_proposal_states.c.status == "ready").order_by(t.plan_proposals.c.created_at, t.plan_proposals.c.id)
        if input_hash is not None:
            query = query.where(t.plan_proposal_states.c.input_hash == _hash(input_hash))
        return [self.get(user_id, identifier) for identifier in self.connection.scalars(query)]

    def result(self, user_id, proposal_id) -> dict:
        state = self.get(user_id, proposal_id)
        if state["legacy"]:
            if state["status"] == "applied":
                raise LegacyResponseUnavailable("The original applied response is unknown; do not re-execute")
            raise LegacyProposalUnavailable("Legacy candidates need a newly generated preview")
        if state["status"] != "applied":
            raise ProposalConflict("The proposal has no applied result")
        return state["result_response"]

    def create(self, user_id, *, kind: str, target: dict | str, normalized_input: dict | str,
               base_date: date, dependency_versions: dict | str, changes: dict | str,
               preview_response: dict | str, now: datetime, proposal_type: str, description: str,
               base_plan_id=None, base_plan_revision=None, base_structure_hash=None,
               request_id=None, request_text=None, proposal_id=None, preview_schema_version=1) -> dict:
        """Insert one immutable candidate, optionally attaching to an owned ready request.

        No approved plan, task, schedule, goal or request decision is changed.
        New requests and all candidate rows are protected by one savepoint.
        """
        user_id = _uuid(user_id)
        proposal_id = _uuid(proposal_id) if proposal_id is not None else str(uuid4())
        request_id = _uuid(request_id) if request_id is not None else None
        base_plan_id = _uuid(base_plan_id) if base_plan_id is not None else None
        now = _time(now)
        _text(kind, "kind", nonempty=True)
        _text(proposal_type, "proposal_type", nonempty=True)
        _text(description, "description")
        if request_text is not None:
            _text(request_text, "request_text")
        if type(base_date) is not date:
            raise ValueError("A base date is required")
        _positive(preview_schema_version, "preview_schema_version")
        if base_plan_id is not None:
            _positive(base_plan_revision, "base_plan_revision")
            if base_structure_hash is not None:
                raise ValueError("A planned candidate uses a plan revision, not an initial structure hash")
        elif base_plan_revision is not None:
            raise ValueError("A plan revision requires its plan ID")
        else:
            _hash(base_structure_hash)
        target, normalized_input, dependency_versions, changes, preview_response = (
            canonical_object(item) for item in (target, normalized_input, dependency_versions, changes, preview_response))
        if not target:
            raise ValueError("An explicit target is required")
        input_hash = canonical_hash(normalized_input)

        # SQLite's user UPDATE also starts the real outer transaction before a
        # SAVEPOINT; releasing it must never commit a caller's uncommitted work.
        self._lock(user_id)
        with self.connection.begin_nested():
            if base_plan_id is not None and self.connection.scalar(select(t.plans.c.id).where(
                    t.plans.c.id == base_plan_id, t.plans.c.user_id == user_id)) is None:
                raise ProposalNotFound(foreign=True)
            if request_id is not None:
                request = self._request(user_id, request_id)
                if (request["kind"], request["base_plan_id"], request["base_plan_revision"]) != (
                        kind, base_plan_id, base_plan_revision):
                    raise ProposalConflict("Candidate kind or basis does not match its request")
                if request["status"] != "ready" or request["accepted_proposal_id"] is not None:
                    raise ProposalConflict("A candidate can only join a ready, unselected request")
            else:
                request_id = str(uuid4())
                self.connection.execute(t.plan_change_requests.insert().values(
                    id=request_id, user_id=user_id, kind=kind, status="ready", base_plan_id=base_plan_id,
                    base_plan_revision=base_plan_revision, input_payload=normalized_input, request_text=request_text,
                    created_at=now, updated_at=now))
            self.connection.execute(t.plan_proposals.insert().values(
                id=proposal_id, request_id=request_id, proposal_type=proposal_type, description=description,
                changes=changes, created_at=now))
            self.connection.execute(t.plan_proposal_states.insert().values(
                proposal_id=proposal_id, user_id=user_id, kind=kind, target=target, normalized_input=normalized_input,
                input_hash=input_hash, base_date=base_date, base_plan_id=base_plan_id,
                base_plan_revision=base_plan_revision, base_structure_hash=base_structure_hash,
                dependency_versions=dependency_versions, status="ready", state_revision=0,
                preview_response=preview_response, preview_schema_version=preview_schema_version,
                result_response=null(), result_schema_version=None, result_captured_at=None, decided_at=None,
                legacy=False, legacy_snapshot=null()))
            return self.get(user_id, proposal_id)
