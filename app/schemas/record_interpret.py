"""Interpretation is a read-only page candidate, never a record command."""
from pydantic import BaseModel, ConfigDict, field_validator
from app.schemas.records import UUIDString
from app.core.responses import ApiError


class InterpretRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    taskId: UUIDString
    text: str

    @field_validator('text')
    @classmethod
    def valid_text(cls, value):
        if not value.strip():
            raise ApiError('EMPTY', '공부한 내용을 입력해 주세요', 400)
        if '\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('Unsupported text')
        return value
