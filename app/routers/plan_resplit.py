"""Explicit resplit request, comparison, approval, cancellation and rebuild."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.plan_resplit import ResplitRequest, ResplitApproval
from app.schemas.units import UUIDString
from app.services.plan_resplit import PlanResplitService

router=APIRouter(prefix='/api/plan',tags=['계획 재분할'])


def _service(conn):
    engine=conn.engine
    conn.close()
    return PlanResplitService(engine)


@router.post('/resplit')
def request_resplit(body:ResplitRequest,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(_service(conn).prepare(user['id'],body.request,answer=body.answer,request_id=body.requestId,saved=body.saved))


@router.get('/resplit')
def compare_resplit(previewId:UUIDString|None=None,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(_service(conn).view(user['id'],previewId))


@router.delete('/resplit')
def cancel_resplit(previewId:Annotated[UUIDString, Query()],user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(_service(conn).cancel(user['id'],previewId))


@router.post('/resplit/apply')
def apply_resplit(body:ResplitApproval,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(_service(conn).apply(user['id'],body.previewId))
