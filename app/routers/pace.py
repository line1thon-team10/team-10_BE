from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.records import UUIDString
from app.services.pace import PaceService

router=APIRouter(prefix='/api/plan',tags=['속도 보정'])


class PaceDecision(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    subjectId: UUIDString
    calibrationId: UUIDString
    apply: bool


@router.post('/pace')
def pace_decision(body:PaceDecision,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(PaceService(conn,user).decide(body.subjectId,body.calibrationId,body.apply))
