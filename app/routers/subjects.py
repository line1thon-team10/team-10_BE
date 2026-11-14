from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.services.subjects import SubjectService

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
