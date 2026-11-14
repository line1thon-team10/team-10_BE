from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, model_validator
from pydantic.json_schema import SkipJsonSchema

from app.core.responses import ApiError


class QuizCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Answer(QuizCommand):
    questionId: UUID
    choice: StrictInt | None = None
    text: StrictStr | None = None
    confidence: Literal["sure", "unsure"]

    @model_validator(mode="before")
    @classmethod
    def require_confidence(cls, data: Any) -> Any:
        # 필수 필드의 OpenAPI 계약을 유지하면서 누락 전용 오류를 반환한다.
        if isinstance(data, dict) and "confidence" not in data:
            raise ApiError("CONFIDENCE_REQUIRED", "모든 문항에 확실/애매를 골라 주세요")
        return data


class SubmitIn(QuizCommand):
    quizId: UUID | SkipJsonSchema[None] = None
    answers: list[Answer]
    savedGrading: StrictBool = False

    @model_validator(mode="before")
    @classmethod
    def optional_quiz_id_must_be_valid_when_present(cls, data: Any) -> Any:
        if isinstance(data, dict) and "quizId" in data and data["quizId"] is None:
            raise ApiError("INVALID_INPUT", "퀴즈 ID는 생략하거나 유효한 ID로 보내 주세요", 422)
        return data
