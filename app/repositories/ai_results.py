"""Short, engine-owned cache transactions; never close a caller's connection."""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Engine, select

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

    def _assert_owner(self, conn, request):
        from app.ai.gateway import GatewayRequest
        if not isinstance(request, GatewayRequest):
            raise TypeError("GatewayRequest is required")
        if conn.scalar(select(t.users.c.id).where(t.users.c.id == request.owner_id)) is None:
            raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
        wanted = {item.material_id for item in request.identity.materials}
        if wanted:
            owned = set(conn.scalars(select(t.materials.c.id).join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
                                    .where(t.materials.c.id.in_(wanted), t.subjects.c.user_id == request.owner_id)))
            if owned != wanted:
                raise ApiError("NO_MATERIAL", "자료를 찾지 못했어요", 404)
        if request.job_id is not None and conn.scalar(select(t.ai_jobs.c.id).where(
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
        from app.ai.gateway import DemoApproval, canonical_json, parse_json
        if type(result) not in (dict, list):
            raise ValueError("A validated AI result must be an object or array")
        snapshot = parse_json(canonical_json(result))
        identity, owner_id = request.identity, request.owner_id
        if demo_approval is not None:
            if not isinstance(demo_approval, DemoApproval) or not demo_approval.matches(identity) or identity.materials:
                raise ValueError("Shared cache is restricted to the exact approved self-authored demo")
            owner_id = None
        result_id, now = str(uuid4()), datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            self._assert_owner(conn, request)
            conn.execute(t.ai_saved_results.insert().values(
                id=result_id, user_id=owner_id, scenario_key=identity.scenario_key, kind=identity.kind,
                input_hash=identity.input_hash, schema_version=identity.schema_version, result=snapshot,
                validated_at=now, created_at=now))
        return result_id
