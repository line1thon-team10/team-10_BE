from fastapi import APIRouter,Depends
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.repositories.record_requests import record_request_key
from app.schemas.relearning import RelearnIn
from app.services.relearning import RelearningService

router=APIRouter(prefix='/api/review',tags=['재학습'])


@router.post('/relearn')
def relearn(body:RelearnIn,user:dict=Depends(current_user),conn:Connection=Depends(get_conn),key=Depends(record_request_key)):
    return ok(RelearningService(conn,user).complete(body,key))
