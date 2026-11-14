"""Bounded calendar query contract."""
from datetime import date
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.onboarding_events import LocalDate


class CalendarRange(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    from_date: LocalDate = Field(alias='from')
    to: LocalDate

    @model_validator(mode='after')
    def bounded(self):
        if not 0 <= (self.to - self.from_date).days < 42:
            raise ValueError('Calendar range must include between 1 and 42 days')
        return self
