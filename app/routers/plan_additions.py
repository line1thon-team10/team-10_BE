from fastapi import APIRouter, Depends
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.plan_additions import AdditionPreview, AdditionApproval
from app.services.plan_additions import PlanAdditionsService

router=APIRouter(prefix='/api/plan/additions',tags=['계획 범위 추가'])


@router.post('/preview')
def preview(body:AdditionPreview,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine
    conn.close()
    return ok(PlanAdditionsService(engine).preview(user['id'],body))


@router.post('')
def apply(body:AdditionApproval,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine
    conn.close()
    return ok(PlanAdditionsService(engine).apply(user['id'],body.previewId))
