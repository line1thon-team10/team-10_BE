from pydantic import BaseModel, ConfigDict, field_validator
from app.schemas.plan_resplit import ResplitRequest


class RebuildRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request: str | None = None
    saved: bool = False

    @field_validator('request')
    @classmethod
    def text(cls, value):
        return ResplitRequest.text(value)

