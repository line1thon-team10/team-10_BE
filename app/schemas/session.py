from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionData(BaseModel):
    anonymousId: UUID


class SessionResponse(BaseModel):
    success: Literal[True] = True
    data: SessionData
