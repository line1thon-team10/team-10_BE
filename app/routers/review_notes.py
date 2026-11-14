from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.clock import today_for
from app.core.responses import ok
from app.db.session import get_conn
from app.services.review_notes import ReviewNotesService
from app.schemas.review import RetryIn
from app.services.review_retry import ReviewRetryService

router=APIRouter(prefix='/api/review/notes',tags=['오답 노트'])


@router.get('')
def notes(group: Literal['date','unit','count'],user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(ReviewNotesService(conn,user,today_for(user)).grouped(group))


@router.get('/{note_id}/question')
def question(note_id:UUID,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(ReviewNotesService(conn,user,today_for(user)).question(str(note_id)))


@router.post('/{note_id}/retry')
def retry(note_id:UUID,body:'RetryIn',user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine
    conn.close()
    return ok(ReviewRetryService(engine).run(user['id'],str(note_id),body))
