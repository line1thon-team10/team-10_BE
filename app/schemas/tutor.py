from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.responses import ApiError
from app.schemas.units import UUIDString


class TutorPage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    materialId: UUIDString
    from_: int = Field(alias='from', ge=1)
    to: int = Field(ge=1)


class TutorHistory(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    question: str
    answer: str
    pages: list[TutorPage]


class TutorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    question: str
    history: list[TutorHistory] = Field(default_factory=list, max_length=1)

    @field_validator('question')
    @classmethod
    def valid_question(cls, value):
        if not value.strip():
            raise ApiError('EMPTY', '질문을 입력해 주세요', 400)
        if '\x00' in value:
            raise ValueError('A question must not contain NUL')
        value.encode('utf-8')
        return value.strip()


class TutorAnswer(BaseModel):
    id: UUIDString
    subjectId: UUIDString
    subjectName: str
    question: str
    answer: str
    parentId: UUIDString | None
    pages: list[TutorPage]
    saved: bool
    mockFilled: list[str] = Field(default_factory=list)


class TutorResponse(BaseModel):
    success: Literal[True] = True
    data: TutorAnswer
