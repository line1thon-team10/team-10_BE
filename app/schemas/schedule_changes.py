from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class DeletePreviewQuery(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scope: Literal['once','future']
    expectedRevision: int = Field(ge=1)
