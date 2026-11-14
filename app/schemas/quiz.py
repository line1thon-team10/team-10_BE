from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from app.core.responses import ApiError
from app.domain.quiz_answers import validate_answer_text


class QuizCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Answer(QuizCommand):
    questionId: UUID
    choice: StrictInt | None = None
    text: StrictStr | None = None
    confidence: Literal["sure", "unsure"]

    @field_validator("text")
    @classmethod
    def require_storable_text(cls, value: str | None) -> str | None:
        return validate_answer_text(value)

    @model_validator(mode="before")
    @classmethod
    def require_confidence(cls, data: Any) -> Any:
        # 필수 필드의 OpenAPI 계약을 유지하면서 누락 전용 오류를 반환한다.
        if isinstance(data, dict) and "confidence" not in data:
            raise ApiError("CONFIDENCE_REQUIRED", "모든 문항에 확실/애매를 골라 주세요")
        return data


class SubmitIn(QuizCommand):
    quizId: UUID
    answers: list[Answer]
    savedGrading: StrictBool = False
