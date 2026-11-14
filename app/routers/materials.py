from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import Connection
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from app.core.auth import current_user
from app.core.responses import ApiError, ok
from app.db.session import get_conn
from app.schemas.materials import MaterialsResponse, MaterialUploadResponse, MaterialJobResponse
from app.services.material_storage import LimitedPdfParser, MaterialStorageService


router = APIRouter(prefix="/api/materials", tags=["자료"])


def _storage(conn):
    engine = conn.engine
    conn.close()  # Release the auth transaction before file I/O or streaming.
    return MaterialStorageService(engine)


@router.get("", response_model=MaterialsResponse)
def list_materials(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(_storage(conn).list_materials(user["id"]))


@router.post("", response_model=MaterialUploadResponse, openapi_extra={"requestBody": {
    "required": True, "content": {"multipart/form-data": {"schema": {"type": "object",
        "required": ["subjectId", "file"], "additionalProperties": False,
        "properties": {"subjectId": {"type": "string", "format": "uuid"},
                       "file": {"type": "string", "format": "binary"}}}}}}})
async def upload_material(request: Request, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    service = _storage(conn)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise ApiError("INVALID_INPUT", "과목 ID와 PDF 파일을 multipart로 보내 주세요", 422)
    parser = LimitedPdfParser(request.headers, request.stream(), limit=service.max_upload_bytes)
    form = None
    try:
        try:
            form = await parser.parse()
        except MultiPartException:
            raise ApiError("INVALID_INPUT", "업로드 형식이 올바르지 않아요", 422) from None
        if sorted(form.keys()) != ["file", "subjectId"] or not isinstance(form["file"], UploadFile):
            raise ApiError("INVALID_INPUT", "과목 ID와 PDF 파일이 필요해요", 422)
        try:
            subject_id = str(UUID(form["subjectId"]))
        except (ValueError, TypeError, AttributeError):
            raise ApiError("INVALID_INPUT", "과목 ID가 올바르지 않아요", 422) from None
        file = form["file"]
        result = await run_in_threadpool(service.upload, user["id"], subject_id, file.file,
                                        filename=file.filename, content_type=file.content_type)
        return ok(result)
    finally:
        if form is not None:
            for value in form.values():
                if isinstance(value, UploadFile):
                    value.file.close()


@router.get("/{material_id}/file")
def get_file(material_id: UUID, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    path, filename = _storage(conn).original_file(user["id"], str(material_id))
    return FileResponse(path, media_type="application/pdf", filename=filename, content_disposition_type="inline")


@router.get("/jobs/{job_id}", response_model=MaterialJobResponse)
def get_job(job_id: UUID, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    from app.services.material_analysis import MaterialAnalysisService
    engine = conn.engine
    conn.close()
    return ok(MaterialAnalysisService(engine).view(user["id"], str(job_id)))
