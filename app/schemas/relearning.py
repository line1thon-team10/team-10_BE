from uuid import UUID
from pydantic import BaseModel,ConfigDict


class RelearnIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    subjectId: UUID
    unitId: UUID
