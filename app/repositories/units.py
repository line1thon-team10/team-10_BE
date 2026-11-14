"""Owned material structure persistence; historical rows are never deleted."""
from sqlalchemy import Connection, select

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.materials import MaterialRepository


class UnitRepository:
    def __init__(self, connection: Connection):
        self.connection = connection

    def material(self, user_id: str, subject_id: str, material_id: str) -> dict:
        material = MaterialRepository(self.connection).material(user_id, material_id)
        if material["subject_id"] != subject_id:
            raise ApiError("INVALID_INPUT", "과목에 속한 자료를 선택해 주세요", 422)
        return dict(material)

    def active_structure(self, material_id: str) -> tuple[list[dict], list[dict]]:
        units = [dict(row) for row in self.connection.execute(select(t.units).where(
            t.units.c.material_id == material_id, t.units.c.retired_at.is_(None)
        ).order_by(t.units.c.sort_order, t.units.c.id)).mappings()]
        concepts = [dict(row) for row in self.connection.execute(select(t.concepts).join(
            t.units, t.concepts.c.unit_id == t.units.c.id
        ).where(t.units.c.material_id == material_id, t.units.c.retired_at.is_(None),
                t.concepts.c.retired_at.is_(None)
        ).order_by(t.concepts.c.sort_order, t.concepts.c.id)).mappings()]
        return units, concepts
