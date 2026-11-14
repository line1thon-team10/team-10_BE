from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OnboardingQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subjectId: str | None = None


class OnboardingMessage(BaseModel):
    role: Literal["ai", "user"]
    text: str


class ConfirmedFields(BaseModel):
    examDate: bool
    goal: bool
    goalTotal: bool
    goalEtc: bool


class OnboardingSubject(BaseModel):
    id: str
    name: str
    examDate: date | None
    goal: str | None
    goalTotal: str | None
    goalEtc: str | None
    target: float | None = Field(ge=0, le=100, allow_inf_nan=False)
    status: Literal["pending", "asking", "confirmed"]
    confirmedFields: ConfirmedFields


class OnboardingEvent(BaseModel):
    id: str
    kind: Literal["class", "job", "meet", "other"]
    title: str
    days: list[int] | None
    date: date | None
    start: str
    end: str
    added: bool


class OnboardingState(BaseModel):
    messages: list[OnboardingMessage]
    chips: list[str]
    subjects: list[OnboardingSubject]
    schedules: list[OnboardingEvent]
    done: bool


class OnboardingResponse(BaseModel):
    success: Literal[True] = True
    data: OnboardingState
