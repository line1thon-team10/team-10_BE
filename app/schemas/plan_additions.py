"""Explicit page selection with exactly one daily-time or total-page goal."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.onboarding_events import LocalDate, Weekdays
from app.schemas.units import UUIDString


class AdditionPreview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    subjectId: UUIDString
    materialId: UUIDString
    from_: int = Field(alias='from', ge=1, le=2_147_483_647)
    to: int = Field(ge=1, le=2_147_483_647)
    minutesPerDay: int | None = Field(default=None, ge=1, le=2_147_483_647)
    totalPages: int | None = Field(default=None, ge=1, le=2_147_483_647)
    days: Weekdays
    startDate: LocalDate
    endDate: LocalDate

    @model_validator(mode='after')
    def goal_and_range(self):
        if self.from_ > self.to or self.startDate > self.endDate:
            raise ValueError('Page and date bounds must be ordered')
        if (self.minutesPerDay is None) == (self.totalPages is None):
            raise ValueError('Choose exactly one of minutesPerDay and totalPages')
        if self.totalPages is not None and self.totalPages > self.to-self.from_+1:
            raise ValueError('Page goal cannot exceed the selected range')
        return self


class AdditionApproval(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    previewId: UUIDString
