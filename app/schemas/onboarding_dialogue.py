from uuid import UUID
from pydantic import BaseModel, ConfigDict, field_validator

from app.core.responses import ApiError


class DialogueCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str
    subjectId: str | None = None

    @field_validator('text')
    @classmethod
    def valid_text(cls, value):
        if not value.strip():
            raise ApiError('EMPTY', '내용을 입력해 주세요', 400)
        if '\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('Unsupported text')
        return value

    @field_validator('subjectId')
    @classmethod
    def valid_subject(cls, value):
        return str(UUID(value)) if value is not None else None


class VoiceCommand(DialogueCommand):
    @field_validator('subjectId')
    @classmethod
    def no_subject(cls, value):
        if value is not None:
            raise ValueError('Calendar voice input is initial-onboarding only')
        return value
