from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FocusCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FocusStart(FocusCommand):
    taskId: UUID


class FocusRange(BaseModel):
    id: UUID
    materialId: UUID
    unitId: UUID
    conceptId: UUID | None
    from_: int = Field(alias="from")
    to: int
    doneTo: int | None


class FocusTask(BaseModel):
    title: str
    from_: int | None = Field(alias="from")
    to: int | None
    pages: int = Field(ge=0)
    minutes: float = Field(gt=0)
    ranges: list[FocusRange]


class FocusMaterial(BaseModel):
    id: UUID
    page: int = Field(ge=1)


class FocusState(BaseModel):
    id: UUID
    taskId: UUID
    subjectId: UUID
    subjectName: str
    task: FocusTask
    status: Literal["active", "paused", "ended"]
    quote: str
    material: FocusMaterial | None
    minutes: int = Field(ge=0)
    pausedMinutes: int = Field(ge=0)
    todayMinutes: int = Field(ge=0)
    subjectMinutes: int = Field(ge=0)
    checkIn: bool
    autoStopped: bool
    excludeFromPace: bool
    recorded: bool


class FocusResponse(BaseModel):
    success: Literal[True] = True
    data: FocusState | None
