"""Material files and durable analysis admission, without running analysis."""
from pathlib import Path
import hashlib
import os
import tempfile
from uuid import UUID, uuid4

import pymupdf
from sqlalchemy import Engine
from starlette.formparsers import MultiPartException, MultiPartParser

from app.config import STORAGE_DIR
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.materials import MaterialRepository


class LimitedPdfParser(MultiPartParser):
    """Enforce the actual file bytes while receiving multipart, before spooling."""
    def __init__(self, headers, stream, *, limit):
        super().__init__(headers, stream, max_files=1, max_fields=1)
        self.limit, self.received, self.finished = limit, 0, False

    def on_headers_finished(self):
        super().on_headers_finished()
        if self._current_part.file is not None:
            _require_pdf_mime(self._current_part.file.content_type)

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.received += end - start
            if self.received > self.limit:
                raise ApiError("FILE_TOO_LARGE", "PDF 업로드 용량 제한을 넘었어요", 413)
        super().on_part_data(data, start, end)

    def on_end(self):
        self.finished = True
        super().on_end()

    async def parse(self):
        try:
            form = await super().parse()
            if not self.finished:
                raise MultiPartException("The multipart body is incomplete")
            return form
        except BaseException:
            for file in self._files_to_close_on_error:
                file.close()
            raise


def _require_pdf_mime(content_type):
    if not content_type or content_type.split(";", 1)[0].strip().lower() != "application/pdf":
        raise ApiError("NOT_PDF", "PDF 파일만 올릴 수 있어요", 415)


class MaterialStorageService:
    def __init__(self, engine, *, storage_dir=None, max_upload_bytes=None):
        if not isinstance(engine, Engine):
            raise TypeError("Pass an Engine for short material transactions")
        self.engine = engine
        self.root = Path(storage_dir if storage_dir is not None else STORAGE_DIR).resolve()
        limit = int(os.environ.get("MAX_PDF_UPLOAD_BYTES", "26214400")) if max_upload_bytes is None else max_upload_bytes
        if type(limit) is not int or limit <= 0:
            raise ValueError("MAX_PDF_UPLOAD_BYTES must be a positive integer")
        self.max_upload_bytes = limit

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

    def _receive(self, source):
        temporary_dir = self.root / ".uploads"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        if temporary_dir.resolve() != temporary_dir:
            raise OSError("Upload staging cannot follow a symlink")
        handle, name = tempfile.mkstemp(prefix="pdf-", suffix=".part", dir=temporary_dir)
        path, digest, size = Path(name), hashlib.sha256(), 0
        try:
            with os.fdopen(handle, "wb") as target:
                while chunk := source.read(min(65536, self.max_upload_bytes - size + 1)):
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ApiError("FILE_TOO_LARGE", "PDF 업로드 용량 제한을 넘었어요", 413)
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            return path, digest.hexdigest()
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _pages(path):
        with path.open("rb") as source:
            if not source.read(5) == b"%PDF-":
                raise ApiError("NOT_PDF", "PDF 형식의 파일이 아니에요", 415)
        try:
            with pymupdf.open(path) as document:
                if not document.is_pdf or document.needs_pass or document.is_encrypted or document.is_repaired:
                    raise ValueError("PDF requires a password or repair")
                if document.page_count <= 0:
                    raise ValueError("PDF has no readable pages")
                for page in document:
                    _ = page.rect
                    for xref in page.get_contents():
                        document.xref_stream(xref)
                if document.is_repaired:
                    raise ValueError("PDF required repair while reading pages")
                return document.page_count
        except Exception as error:
            raise ApiError("BAD_REQUEST", "잠금 또는 손상으로 PDF를 읽지 못했어요", 400) from error

    def _publish(self, temporary, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() != destination:
            raise OSError("Material storage cannot follow a symlink")
        os.replace(temporary, destination)
        parent = destination.parent
        while True:
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if parent == self.root:
                break
            parent = parent.parent

    def _remove_uncommitted(self, user_id, material_id, path, key):
        # A disconnect at COMMIT can have an unknown outcome. Never delete a
        # file referenced by a committed row; a retry can recover that job ID.
        try:
            with self.engine.connect() as conn:
                try:
                    material = MaterialRepository(conn).material(user_id, material_id)
                except ApiError as error:
                    if error.status != 404:
                        raise
                    material = None
        except Exception:
            # If DB state cannot be checked, preserving the original is safer
            # than leaving a committed analysis job without its input file.
            return
        if material is None or material["original_storage_key"] != key:
            path.unlink(missing_ok=True)

    def upload(self, user_id, subject_id, source, *, filename, content_type):
        _require_pdf_mime(content_type)
        filename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        try:
            filename.encode("utf-8")
            if not filename.strip() or filename in (".", "..") or any(ord(char) < 32 for char in filename):
                raise ValueError
        except (UnicodeError, ValueError):
            raise ApiError("INVALID_INPUT", "파일 이름이 올바르지 않아요", 422) from None
        with self.engine.connect() as conn:
            MaterialRepository(conn).subject(user_id, subject_id)
        temporary, digest = self._receive(source)
        created_path, material_id, key = None, None, None
        try:
            pages = self._pages(temporary)
            with self.engine.begin() as conn:
                try:
                    user = lock_user(conn, UUID(user_id))
                except UserBusyError:
                    raise ApiError("REQUEST_IN_PROGRESS", "자료 저장이 진행 중이에요. 다시 시도해 주세요", 409) from None
                if user is None:
                    raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
                repo = MaterialRepository(conn)
                matches = repo.matching(user_id, subject_id, digest)
                material, reusable = None, None
                for candidate in matches:
                    job = repo.latest_job(user_id, candidate["id"])
                    if job and job["status"] in ("queued", "running", "succeeded", "done"):
                        material, reusable = candidate, job
                        break
                material = material or (matches[0] if matches else None)
                material_id = material["id"] if material else str(uuid4())
                key = self.storage_key(user_id, material_id, digest)
                path = self.root / key
                if not path.exists():
                    created_path = path
                self._publish(temporary, path)
                now = now_utc()
                if material is None:
                    repo.add(material_id=material_id, subject_id=subject_id, filename=filename,
                             sha256=digest, key=key, pages=pages, now=now)
                if reusable is not None:
                    repo.restore_original(user_id, material_id, key=key, pages=pages, now=now)
                    result = {"materialId": material_id, "jobId": reusable["id"]}
                else:
                    result = {"materialId": material_id, "jobId": repo.enqueue(user_id, material_id,
                        key=key, pages=pages, now=now)}
            return result
        except BaseException:
            if created_path is not None:
                self._remove_uncommitted(user_id, material_id, created_path, key)
            raise
        finally:
            temporary.unlink(missing_ok=True)
