"""Short, engine-owned cache transactions; never close a caller's connection."""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import IntegrityError

from app.core.responses import ApiError
from app.db import schema as t


@dataclass(frozen=True, repr=False)
class SavedAiResult:
    id: str
    result: dict | list
    source: str


class AiResultRepository:
    def __init__(self, engine: Engine):
        if not isinstance(engine, Engine):
            raise TypeError("Pass an Engine; the gateway does not own caller Connections")
        self.engine = engine

    def _assert_owner(self, conn, request, *, check_materials=True, check_job=True):
        from app.ai.gateway import GatewayRequest
        if not isinstance(request, GatewayRequest):
            raise TypeError("GatewayRequest is required")
        if conn.scalar(select(t.users.c.id).where(t.users.c.id == request.owner_id)) is None:
            raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
        wanted = {item.material_id for item in request.identity.materials}
        if wanted and check_materials:
            owned = set(conn.scalars(select(t.materials.c.id).join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
                                    .where(t.materials.c.id.in_(wanted), t.subjects.c.user_id == request.owner_id)))
            if owned != wanted:
                raise ApiError("NO_MATERIAL", "자료를 찾지 못했어요", 404)
        if check_job and request.job_id is not None and conn.scalar(select(t.ai_jobs.c.id).where(
            t.ai_jobs.c.id == request.job_id, t.ai_jobs.c.user_id == request.owner_id,
            t.ai_jobs.c.kind == request.identity.kind,
        )) is None:
            raise ValueError("The AI job does not match this owner and operation")

    def lookup(self, request, *, demo_approval=None) -> SavedAiResult | None:
        from app.ai.gateway import DemoApproval
        if demo_approval is not None and not isinstance(demo_approval, DemoApproval):
            raise TypeError("A server-owned DemoApproval is required")
        identity = request.identity
        table = t.ai_saved_results
        predicate = (table.c.kind == identity.kind, table.c.input_hash == identity.input_hash,
                     table.c.schema_version == identity.schema_version, table.c.scenario_key == identity.scenario_key)
        with self.engine.connect() as conn:
            self._assert_owner(conn, request)
            row = conn.execute(select(table).where(*predicate, table.c.user_id == request.owner_id)
                               .order_by(table.c.validated_at.desc(), table.c.id).limit(1)).mappings().first()
            source = "private_cache"
            if row is None and demo_approval is not None and demo_approval.matches(identity) and not identity.materials:
                row = conn.execute(select(table).where(*predicate, table.c.user_id.is_(None))
                                   .order_by(table.c.validated_at.desc(), table.c.id).limit(1)).mappings().first()
                source = "demo_cache"
            return SavedAiResult(row["id"], row["result"], source) if row else None

    def save(self, request, result, *, demo_approval=None) -> str:
        with self.engine.begin() as conn:
            return self.save_in_transaction(conn, request, result, demo_approval=demo_approval)

    def save_in_transaction(self, conn, request, result, *, demo_approval=None) -> str:
        """Save validated setup data without committing or closing the caller."""
        from app.ai.gateway import DemoApproval, canonical_json, parse_json
        if not isinstance(conn, Connection):
            raise TypeError("Pass the caller's transaction Connection")
        if type(result) not in (dict, list):
            raise ValueError("A validated AI result must be an object or array")
        snapshot = parse_json(canonical_json(result))
        identity, owner_id = request.identity, request.owner_id
        if demo_approval is not None:
            if not isinstance(demo_approval, DemoApproval) or not demo_approval.matches(identity) or identity.materials:
                raise ValueError("Shared cache is restricted to the exact approved self-authored demo")
            owner_id = None
        result_id, now = str(uuid4()), datetime.now(timezone.utc)
        self._assert_owner(conn, request)
        conn.execute(t.ai_saved_results.insert().values(
            id=result_id, user_id=owner_id, scenario_key=identity.scenario_key, kind=identity.kind,
            input_hash=identity.input_hash, schema_version=identity.schema_version, result=snapshot,
            validated_at=now, created_at=now))
        return result_id

    def record_usage(self, request, usage) -> None:
        """Persist observed attempts even when subsequent result validation/save fails."""
        from app.ai.gateway import UsageRecord, _Telemetry
        records = tuple(usage)
        if not records:
            return
        checked = _Telemetry()
        for row in records:
            if not isinstance(row, UsageRecord) or not isinstance(row.created_at, datetime) or row.created_at.utcoffset() is None:
                raise ValueError("An observed transport record with an aware timestamp is required")
            checked.accept({"type": "request_started", "id": row.id, "provider": row.provider, "model": row.model})
            checked.accept({"type": "request_finished", "id": row.id, "status": row.status,
                "provider_request_id": row.provider_request_id, "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens, "estimated_cost": row.estimated_cost,
                "currency": row.currency, "latency_ms": row.latency_ms})
        def rows(job_id):
            return [{
                "id": row.id, "job_id": job_id, "user_id": request.owner_id, "kind": request.identity.kind,
                "provider": row.provider, "model": row.model, "provider_request_id": row.provider_request_id,
                "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
                "estimated_cost": row.estimated_cost, "currency": row.currency, "latency_ms": row.latency_ms,
                "status": row.status, "created_at": row.created_at,
            } for row in records]

        job_id = None
        try:
            with self.engine.begin() as conn:
                # Materials/jobs can be deleted during an external request.
                self._assert_owner(conn, request, check_materials=False, check_job=False)
                if request.job_id is not None:
                    job_id = conn.scalar(select(t.ai_jobs.c.id).where(
                        t.ai_jobs.c.id == request.job_id, t.ai_jobs.c.user_id == request.owner_id,
                        t.ai_jobs.c.kind == request.identity.kind))
                conn.execute(t.ai_usage_logs.insert(), rows(job_id))
        except IntegrityError:
            if job_id is None:
                raise
            # A concurrent reset may delete the observed job after its SELECT.
            # The first transaction has rolled back the whole batch. Retry once
            # without that optional FK only if a fresh transaction proves deletion.
            with self.engine.begin() as conn:
                if conn.scalar(select(t.ai_jobs.c.id).where(t.ai_jobs.c.id == job_id)) is not None:
                    raise
                self._assert_owner(conn, request, check_materials=False, check_job=False)
                conn.execute(t.ai_usage_logs.insert(), rows(None))
