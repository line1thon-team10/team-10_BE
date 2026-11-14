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


class MaterialAccepted(BaseModel):
    materialId: UUID
    jobId: UUID


class MaterialUploadResponse(BaseModel):
    success: Literal[True] = True
    data: MaterialAccepted


class MaterialJob(BaseModel):
    jobId: UUID
    materialId: UUID
    subjectId: UUID
    status: Literal['running', 'done', 'failed']
    ocr: bool | None
    steps: list[str]
    current: int
    failedStep: int | None
    progress: int
    reused: bool
    reason: str | None


class MaterialJobResponse(BaseModel):
    success: Literal[True] = True
    data: MaterialJob
