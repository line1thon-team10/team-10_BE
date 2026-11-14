from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.schedule_changes import DeletePreviewQuery
from app.services.schedule_changes import ScheduleChangesService

router=APIRouter(prefix='/api/schedules',tags=['일정 변경'])


@router.delete('/{occurrence_id}/preview')
def delete_preview(occurrence_id:str,query:Annotated[DeletePreviewQuery,Query()],user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine;conn.close()
    return ok(ScheduleChangesService(engine).delete_preview(user['id'],occurrence_id,query.scope,query.expectedRevision))


@router.delete('/{occurrence_id}')
def delete_schedule(occurrence_id:str,previewId:UUID,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine;conn.close()
    return ok(ScheduleChangesService(engine).apply(user['id'],occurrence_id,str(previewId)))
