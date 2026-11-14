from uuid import UUID
from pydantic import BaseModel, ConfigDict


class PlanApproval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    previewId: UUID
