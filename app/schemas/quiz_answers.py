"""Public answer input contains no grading controls or result fields."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.core.responses import ApiError


class SaveAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: StrictInt | None = None
    text: StrictStr | None = None
    confidence: Literal["sure", "unsure"]
    revision: StrictInt = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def require_confidence(cls, data: Any) -> Any:
        if isinstance(data, dict) and "confidence" not in data:
            raise ApiError("CONFIDENCE_REQUIRED", "확실/애매를 골라 주세요")
        return data
