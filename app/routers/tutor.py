from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.tutor import TutorRequest, TutorResponse
from app.services.tutor import TutorService

router = APIRouter(prefix='/api/tutor', tags=['튜터'])


@router.post('', response_model=TutorResponse)
def ask_tutor(body: TutorRequest, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(TutorService(engine).ask(user['id'], body))


@router.get('/backup', response_model=TutorResponse)
def backup_tutor(kind: Literal['basic', 'easy'] = 'basic', user: dict = Depends(current_user),
                 conn: Connection = Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(TutorService(engine).backup(user['id'], kind))
