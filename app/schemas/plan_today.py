"""Today's changes accept a command at preview time and only its ID at apply."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, model_validator

Action = Literal['more30', 'postpone']


class TodayPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Action
    taskId: UUID | None = None

    @model_validator(mode='after')
    def task_only_for_postpone(self):
        if (self.action == 'postpone') != (self.taskId is not None):
            raise ValueError('Only postpone requires a taskId')
        return self


class TodayApply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Action
    previewId: UUID
