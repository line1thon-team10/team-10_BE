"""Occurrence-addressed calendar commands; only previews accept change inputs."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.onboarding_events import Clock, Kind, LocalDate, Title, Weekdays


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class OnceEvent(Strict):
    date: LocalDate
    start: Clock
    end: Clock

    @model_validator(mode='after')
    def ordered(self):
        if self.start>=self.end: raise ValueError('An event must end after it starts')
        return self


class FutureEvent(Strict):
    kind: Kind
    title: Title
    days: Weekdays
    startDate: LocalDate
    start: Clock
    end: Clock
    until: LocalDate | None

    @model_validator(mode='after')
    def ordered(self):
        if self.start>=self.end or (self.until is not None and self.until<self.startDate):
            raise ValueError('The time and recurrence range must be ordered')
        if self.startDate.isoweekday() not in self.days:
            raise ValueError('startDate must be the first selected weekday')
        return self


class ScheduleEdit(Strict):
    scope: Literal['once','future']
    expectedRevision: int = Field(ge=1)
    event: OnceEvent | FutureEvent

    @model_validator(mode='after')
    def matching_scope(self):
        if (self.scope=='once') != isinstance(self.event,OnceEvent):
            raise ValueError('Event fields must match the selected scope')
        return self


class DeletePreviewQuery(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scope: Literal['once','future']
    expectedRevision: int = Field(ge=1)


class Approval(BaseModel):
    model_config=ConfigDict(extra='forbid')
    previewId: UUID
