from fastapi import APIRouter,Depends
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.repositories.record_requests import record_request_key
from app.schemas.review import CustomQuizIn
from app.services.custom_quiz import CustomQuizService

router=APIRouter(prefix='/api/review',tags=['문제 뽑기'])


@router.post('/custom-quiz')
def custom_quiz(body:CustomQuizIn,user:dict=Depends(current_user),conn:Connection=Depends(get_conn),key=Depends(record_request_key)):
    return ok(CustomQuizService(conn,user).create(body,key))
