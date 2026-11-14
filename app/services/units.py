"""Material-local unit identity and public page structure."""
from collections import defaultdict

from app.repositories.units import UnitRepository
from app.services.task_views import unit_no


class UnitService:
    def __init__(self, connection):
        self.connection = connection
        self.repository = UnitRepository(connection)

    def get(self, user_id: str, subject_id: str, material_id: str) -> dict:
        material = self.repository.material(user_id, subject_id, material_id)
        units, concepts = self.repository.active_structure(material_id)
        return self.render(material, units, concepts)

    @staticmethod
    def render(material, units, concepts):
        by_unit = defaultdict(list)
        for concept in concepts:
            by_unit[concept["unit_id"]].append({"id": concept["id"], "title": concept["title"],
                "from": concept["start_page"], "to": concept["end_page"], "inExam": concept["in_scope"]})
        return {"subjectId": material["subject_id"], "materialId": material["id"],
            "file": {"id": material["id"], "name": material["original_name"], "pages": material["page_count"]},
            "units": [{"id": unit["id"], "no": unit_no(unit["code"]), "title": unit["title"],
                "from": unit["start_page"], "to": unit["end_page"], "difficulty": unit["difficulty"],
                "inExam": unit["in_scope"], "quizless": not material["quiz_enabled"] or not unit["evidence"],
                "evidence": unit["evidence"] or [], "concepts": by_unit[unit["id"]]} for unit in units]}
