from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.materials import MaterialsResponse
from app.services.material_storage import MaterialStorageService


router = APIRouter(prefix="/api/materials", tags=["자료"])


def _storage(conn):
    engine = conn.engine
    conn.close()  # Release the auth transaction before file I/O or streaming.
    return MaterialStorageService(engine)


@router.get("", response_model=MaterialsResponse)
def list_materials(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(_storage(conn).list_materials(user["id"]))


@router.get("/{material_id}/file")
def get_file(material_id: UUID, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    path, filename = _storage(conn).original_file(user["id"], str(material_id))
    return FileResponse(path, media_type="application/pdf", filename=filename, content_disposition_type="inline")
