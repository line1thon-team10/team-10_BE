from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictStr


class DemoCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoSeed(DemoCommand):
    seed: StrictStr


class FirstExam(BaseModel):
    subjectId: UUID
    name: str
    examDate: date


class DemoState(BaseModel):
    today: date
    firstExam: FirstExam | None
    seed: Literal["empty", "day9", "overdue"] | None
    seeds: dict[str, str]
    hasPlan: bool
    judgment: Literal["carry", "replan", "none"] | None


class DemoResponse(BaseModel):
    success: Literal[True] = True
    data: DemoState
