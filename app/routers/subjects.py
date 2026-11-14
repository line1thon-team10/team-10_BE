from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.services.subjects import SubjectService
from app.schemas.subject_changes import SubjectChange
from app.schemas.plan_draft import PlanApproval
from app.services.subject_changes import SubjectChangeService
from app.services.subject_archive import SubjectArchiveService

router = APIRouter(prefix='/api/subjects', tags=['과목'])


class SubjectCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str


@router.get('')
def list_subjects(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(SubjectService(conn, user).listing())


@router.post('')
def create_subject(body: SubjectCreate, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(SubjectService(conn, user).create(body.name))


@router.patch('/{subject_id}/preview')
def preview_subject(subject_id: UUID, body: SubjectChange, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(SubjectChangeService(engine).preview(user['id'], str(subject_id), body))


@router.patch('/{subject_id}')
def apply_subject(subject_id: UUID, body: PlanApproval, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(SubjectChangeService(engine).apply(user['id'], str(subject_id), str(body.previewId)))


@router.delete('/{subject_id}/preview')
def preview_archive(subject_id: UUID, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(SubjectArchiveService(engine).preview(user['id'], str(subject_id)))


@router.delete('/{subject_id}')
def archive_subject(subject_id: UUID, previewId: UUID, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(SubjectArchiveService(engine).apply(user['id'], str(subject_id), str(previewId)))
