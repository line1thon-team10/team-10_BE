from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.services.empty_time import EmptyTimeService

router=APIRouter(prefix='/api/plan',tags=['빈 시간'])


class EmptyTimeCommand(BaseModel):
    model_config=ConfigDict(extra='forbid')
    previewId: UUID
    option: Literal['rest','pull','advance']


@router.post('/empty-time')
def apply_empty_time(command:EmptyTimeCommand,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    return ok(EmptyTimeService(conn,user).apply(command.previewId,command.option))
