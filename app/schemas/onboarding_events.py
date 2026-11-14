"""Strict onboarding calendar commands and the shared S2 response shape."""
from datetime import date as Date
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, model_validator

from app.core.responses import ApiError
from app.schemas.onboarding import OnboardingEvent


def _title(value):
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Text must be supported by both SQLite and PostgreSQL")
    if not value.strip():
        raise ApiError("EMPTY", "일정 이름을 입력해 주세요", 400)
    return value


def _date(value):
    if type(value) is Date:
        return value
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("An exact YYYY-MM-DD date is required")
    return Date.fromisoformat(value)


def _clock(value):
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("An exact HH:mm time is required")
    return value


def _days(value):
    if not value or len(value) != len(set(value)):
        raise ValueError("Weekdays must be nonempty and unique")
    return sorted(value)


Title = Annotated[str, AfterValidator(_title)]
LocalDate = Annotated[Date, BeforeValidator(_date)]
Clock = Annotated[str, AfterValidator(_clock)]
Weekday = Annotated[int, Field(strict=True, ge=1, le=7)]
Weekdays = Annotated[list[Weekday], AfterValidator(_days)]
Kind = Literal["class", "job", "meet", "other"]


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Kind
    title: Title
    days: Weekdays | None = None
    date: LocalDate | None = None
    start: Clock
    end: Clock

    @model_validator(mode="after")
    def validate_event(self, info: ValidationInfo):
        if (self.days is None) == (self.date is None):
            raise ValueError("Use exactly one of days or date")
        # A partial edit can retain stored seconds that HH:mm cannot express.
        # Only the service may defer this check, then validate actual instants.
        stored_precision = (info.context or {}).get("validate_stored_instants", False)
        if not stored_precision and self.start >= self.end:
            raise ValueError("An event must start before it ends on the same day")
        return self


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Kind | None = None
    title: Title | None = None
    days: Weekdays | None = None
    date: LocalDate | None = None
    start: Clock | None = None
    end: Clock | None = None

    @model_validator(mode="after")
    def validate_nonnullable_fields(self):
        for name in self.model_fields_set - {"days", "date"}:
            if getattr(self, name) is None:
                raise ValueError("Only days and date can be explicitly null")
        return self


class CalendarQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalendarExam(BaseModel):
    subjectId: str
    name: str
    examDate: Date


class CalendarSummary(BaseModel):
    fixedMinutes: float = Field(ge=0, le=6300)
    freeMinutes: float = Field(ge=0, le=6300)


class OnboardingCalendar(BaseModel):
    weekStart: Date
    today: Date
    events: list[OnboardingEvent]
    exams: list[CalendarExam]
    summary: CalendarSummary
    addedId: str | None


class CalendarResponse(BaseModel):
    success: Literal[True] = True
    data: OnboardingCalendar
