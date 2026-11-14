from fastapi import APIRouter, Body, Depends
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.plan_rebuild import RebuildRequest
from app.services.plan_rebuild import PlanRebuildService

router=APIRouter(prefix='/api/plan',tags=['계획 재생성'])


@router.post('/rebuild')
def rebuild(body:RebuildRequest=Body(default_factory=RebuildRequest),user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine
    conn.close()
    return ok(PlanRebuildService(engine).rebuild(user['id'],body))
