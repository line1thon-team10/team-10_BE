"""Material files and durable analysis admission, without running analysis."""
from pathlib import Path

from sqlalchemy import Engine

from app.config import STORAGE_DIR
from app.core.responses import ApiError
from app.repositories.materials import MaterialRepository


class MaterialStorageService:
    def __init__(self, engine, *, storage_dir=None):
        if not isinstance(engine, Engine):
            raise TypeError("Pass an Engine for short material transactions")
        self.engine = engine
        self.root = Path(storage_dir if storage_dir is not None else STORAGE_DIR).resolve()

    @staticmethod
    def storage_key(user_id, material_id, sha256):
        return f"materials/{user_id}/{material_id}/{sha256}.pdf"

    def list_materials(self, user_id):
        with self.engine.connect() as conn:
            repo = MaterialRepository(conn)
            subjects, materials = repo.listing(user_id)
            output = {subject["id"]: {"id": subject["id"], "name": subject["name"],
                "color": subject["color"], "files": []} for subject in subjects}
            for material in materials:
                job = repo.latest_job(user_id, material["id"])
                state = job["status"] if job else material["analysis_status"]
                done = state in ("done", "succeeded")
                method = material["extraction_method"]
                output[material["subject_id"]]["files"].append({"id": material["id"],
                    "name": material["original_name"], "pages": material["page_count"],
                    "status": "done" if done else "failed" if state in ("failed", "cancelled") else "running",
                    "jobId": job["id"] if job else None,
                    "quizless": not material["quiz_enabled"] if done else None,
                    "ocr": (True if method == "ocr" else False if method in ("text", "native") else None) if done else None})
        return {"subjects": list(output.values())}

    def original_file(self, user_id, material_id):
        with self.engine.connect() as conn:
            material = MaterialRepository(conn).material(user_id, material_id, error_code="NO_FILE")
        key = self.storage_key(user_id, material_id, material["sha256"])
        if material["original_deleted_at"] is not None or material["original_storage_key"] != key:
            raise ApiError("NO_FILE", "원본 PDF를 찾지 못했어요", 404)
        path = self.root / key
        # An owned DB row must never serve a different owner's key or a symlink.
        if path.resolve() != path or not path.is_file():
            raise ApiError("NO_FILE", "원본 PDF를 찾지 못했어요", 404)
        return path, material["original_name"]
