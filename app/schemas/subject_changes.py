"""Explicit subject changes; no coercion or implicit goal confirmation."""
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.core.responses import ApiError
from app.schemas.onboarding_events import LocalDate


class SubjectChange(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    examDate: LocalDate | None = None
    goal: str | None = None
    target: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)

    @field_validator('goal')
    @classmethod
    def goal_text(cls, value):
        if value is None:
            return value
        if not value.strip():
            raise ApiError('EMPTY', '목표를 입력해 주세요', 400)
        if '\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('Unsupported goal text')
        return value.strip()

    @model_validator(mode='after')
    def explicit_change(self):
        if not self.model_fields_set:
            raise ApiError('NO_CHANGE', '변경할 시험일이나 목표를 알려 주세요', 400)
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError('Subject changes cannot clear confirmed values')
        return self
