from uuid import UUID
from pydantic import BaseModel, ConfigDict


class ReplanApproval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str
    previewId: UUID
