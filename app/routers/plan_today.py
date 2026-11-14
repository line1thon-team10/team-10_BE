from fastapi import APIRouter, Depends
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.plan_today import TodayApply, TodayPreview
from app.services.plan_today import TodayPlanService

router=APIRouter(prefix='/api/plan/today',tags=['오늘 계획'])


@router.post('/preview')
def preview(command:TodayPreview,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine;conn.close()
    return ok(TodayPlanService(engine).preview(user['id'],command))


@router.post('')
def apply(command:TodayApply,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine;conn.close()
    return ok(TodayPlanService(engine).apply(user['id'],command))
