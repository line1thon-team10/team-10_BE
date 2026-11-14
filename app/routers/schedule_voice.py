from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.onboarding_dialogue import DialogueCommand
from app.schemas.schedule_changes import Approval
from app.services.schedule_voice import ScheduleVoiceService

router=APIRouter(prefix='/api/schedules/voice',tags=['말로 일정 추가'])


class VoiceText(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    text:str

    @field_validator('text')
    @classmethod
    def supported_text(cls,value):
        return DialogueCommand.valid_text(value)


@router.post('')
def preview_schedule(command:VoiceText,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine;conn.close()
    return ok(ScheduleVoiceService(engine).preview(user['id'],command.text))


@router.post('/apply')
def apply_schedule(command:Approval,user:dict=Depends(current_user),conn:Connection=Depends(get_conn)):
    engine=conn.engine;conn.close()
    return ok(ScheduleVoiceService(engine).apply(user['id'],str(command.previewId)))
