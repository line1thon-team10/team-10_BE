"""A bounded resplit dialogue and server-owned candidate selection."""
from pydantic import BaseModel, ConfigDict, model_validator, field_validator
from app.core.responses import ApiError
from app.schemas.units import UUIDString


class ResplitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request: str
    answer: str | None = None
    requestId: UUIDString | None = None
    saved: bool = False

    @field_validator('request', 'answer')
    @classmethod
    def text(cls, value):
        if value is None:
            return value
        if not value.strip():
            raise ApiError('EMPTY', '다시 나눌 요청을 입력해 주세요', 400)
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError('Invalid text') from None
        if '\x00' in value:
            raise ValueError('Invalid text')
        return value.strip()

    @model_validator(mode='after')
    def followup(self):
        if self.answer is not None and self.requestId is None:
            raise ApiError('INVALID_INPUT', '후속 답변에는 원래 requestId가 필요해요', 422)
        return self


class ResplitApproval(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    previewId: UUIDString
