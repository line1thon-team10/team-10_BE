"""Owned material reads and analysis admission; callers own transactions."""
from sqlalchemy import select

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
