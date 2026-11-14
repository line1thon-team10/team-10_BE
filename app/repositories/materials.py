"""Owned material reads and analysis admission; callers own transactions."""
from datetime import datetime, timezone
import hashlib
from uuid import uuid4

from sqlalchemy import select, update

from app.ai.gateway import canonical_json
from app.core.responses import ApiError
from app.db import schema as t


class MaterialRepository:
    def __init__(self, connection):
        self.connection = connection

    def subject(self, user_id, subject_id):
        row = self.connection.execute(select(t.subjects).where(
            t.subjects.c.id == subject_id, t.subjects.c.user_id == user_id)).mappings().first()
        if row is None:
            raise ApiError("SUBJECT_NOT_FOUND", "과목을 찾지 못했어요", 404)
        return dict(row)

    def material(self, user_id, material_id, *, error_code="NO_MATERIAL"):
        row = self.connection.execute(select(t.materials).join(t.subjects,
            t.materials.c.subject_id == t.subjects.c.id).where(
            t.materials.c.id == material_id, t.subjects.c.user_id == user_id)).mappings().first()
        if row is None:
            raise ApiError(error_code, "자료를 찾지 못했어요", 404)
        return dict(row)

    def latest_job(self, user_id, material_id):
        row = self.connection.execute(select(t.ai_jobs).where(
            t.ai_jobs.c.user_id == user_id, t.ai_jobs.c.kind == "pdf_analysis",
            t.ai_jobs.c.target_type == "material", t.ai_jobs.c.target_id == material_id)
            .order_by(t.ai_jobs.c.created_at.desc(), t.ai_jobs.c.id.desc()).limit(1)).mappings().first()
        return dict(row) if row else None

    def listing(self, user_id):
        subjects = [dict(row) for row in self.connection.execute(select(t.subjects).where(
            t.subjects.c.user_id == user_id).order_by(t.subjects.c.created_at, t.subjects.c.id)).mappings()]
        materials = [dict(row) for row in self.connection.execute(select(t.materials).join(t.subjects,
            t.materials.c.subject_id == t.subjects.c.id).where(t.subjects.c.user_id == user_id)
            .order_by(t.materials.c.created_at, t.materials.c.id)).mappings()]
        return subjects, materials

    def matching(self, user_id, subject_id, sha256):
        self.subject(user_id, subject_id)
        return [dict(row) for row in self.connection.execute(select(t.materials).where(
            t.materials.c.subject_id == subject_id, t.materials.c.sha256 == sha256)
            .order_by(t.materials.c.created_at.desc(), t.materials.c.id.desc())).mappings()]

    def structure_hash(self, user_id, material_id):
        """M-02 rechecks this before replacing any edited units/concepts."""
        self.material(user_id, material_id)
        unit_fields = ("id", "code", "title", "sort_order", "quantity_basis", "start_page", "end_page",
                       "difficulty", "in_scope", "evidence", "retired_at")
        concept_fields = ("id", "unit_id", "code", "title", "sort_order", "start_page", "end_page",
                          "difficulty", "in_scope", "retired_at")
        units = self.connection.execute(select(*(t.units.c[key] for key in unit_fields)).where(
            t.units.c.material_id == material_id).order_by(t.units.c.id)).mappings()
        concepts = self.connection.execute(select(*(t.concepts.c[key] for key in concept_fields))
            .join(t.units, t.concepts.c.unit_id == t.units.c.id).where(t.units.c.material_id == material_id)
            .order_by(t.concepts.c.id)).mappings()
        def rows(values):
            return [{str(key): (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
                     if isinstance(value, datetime) else value for key, value in row.items()} for row in values]
        return hashlib.sha256(canonical_json({"units": rows(units), "concepts": rows(concepts)}).encode()).hexdigest()

    def add(self, *, material_id, subject_id, filename, sha256, key, pages, now):
        self.connection.execute(t.materials.insert().values(id=material_id, subject_id=subject_id,
            original_name=filename, source_format="pdf", sha256=sha256, original_storage_key=key, storage_key=key,
            page_count=pages, analysis_version="material-analysis-v1", analysis_status="pending", analysis_stage="queued",
            created_at=now, updated_at=now))

    def restore_original(self, user_id, material_id, *, key, pages, now):
        """Restore file metadata without replacing analysis or edited structure."""
        self.material(user_id, material_id)
        self.connection.execute(update(t.materials).where(t.materials.c.id == material_id).values(
            original_storage_key=key, storage_key=key, original_deleted_at=None,
            page_count=pages, updated_at=now))

    def enqueue(self, user_id, material_id, *, key, pages, now):
        material = self.material(user_id, material_id)
        job_id = str(uuid4())
        payload = {"schemaVersion": "material-analysis-input-v1", "materialId": material_id,
                   "subjectId": material["subject_id"], "sourceSha256": material["sha256"], "storageKey": key,
                   "analysisVersion": material["analysis_version"], "structureHash": self.structure_hash(user_id, material_id)}
        self.connection.execute(t.ai_jobs.insert().values(id=job_id, user_id=user_id, kind="pdf_analysis",
            status="queued", stage="queued", target_type="material", target_id=material_id,
            idempotency_key=f"material-analysis:{material_id}:{job_id}",
            input_hash=hashlib.sha256(canonical_json(payload).encode()).hexdigest(), input_payload=payload,
            schema_version=payload["schemaVersion"], created_at=now))
        self.connection.execute(update(t.materials).where(t.materials.c.id == material_id).values(
            original_storage_key=key, storage_key=key, original_deleted_at=None, page_count=pages,
            analysis_status="pending", analysis_stage="queued", error_message=None, updated_at=now))
        return job_id
