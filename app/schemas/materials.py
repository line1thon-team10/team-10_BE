from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class MaterialFile(BaseModel):
    id: UUID
    name: str
    pages: int | None
    status: Literal["running", "done", "failed"]
    jobId: UUID | None
    quizless: bool | None
    ocr: bool | None


class MaterialSubject(BaseModel):
    id: UUID
    name: str
    color: str
    files: list[MaterialFile]


class MaterialList(BaseModel):
    subjects: list[MaterialSubject]


class MaterialsResponse(BaseModel):
    success: Literal[True] = True
    data: MaterialList
