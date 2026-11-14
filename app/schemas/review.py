from typing import Literal
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator, model_validator
from app.core.responses import ApiError
from app.domain.quiz_answers import validate_answer_text


class RetryIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    choice: StrictInt | None = None
    text: StrictStr | None = None
    confidence: Literal['sure','unsure']

    @field_validator('text')
    @classmethod
    def storable(cls,value):
        return validate_answer_text(value)

    @model_validator(mode='before')
    @classmethod
    def confidence_required(cls,value):
        if isinstance(value,dict) and 'confidence' not in value:
            raise ApiError('CONFIDENCE_REQUIRED','확실/애매를 골라 주세요')
        return value


from uuid import UUID
from pydantic import Field, model_validator


class PracticeScope(BaseModel):
    model_config=ConfigDict(extra='forbid',populate_by_name=True)
    unitId: UUID
    from_page: StrictInt=Field(alias='from',ge=1)
    to: StrictInt=Field(ge=1)
    conceptId: UUID | None = None

    @model_validator(mode='after')
    def ordered(self):
        if self.from_page>self.to:raise ValueError('Invalid page interval')
        return self


class CustomQuizIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    subjectId: UUID
    scopes: list[PracticeScope]=Field(min_length=1)
    difficulty: Literal['low','mid','high']
    count: StrictInt=Field(ge=1,le=50)
    weight: Literal['even','weak']
