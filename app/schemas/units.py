"""Page-based material structure contracts; IDs are never display numbers."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _uuid_string(value):
    if type(value) is not str:
        raise ValueError("An ID must be a UUID string")
    return str(UUID(value))


UUIDString = Annotated[str, BeforeValidator(_uuid_string)]


class UnitsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subjectId: UUIDString
    materialId: UUIDString


class UnitConcept(BaseModel):
    id: UUIDString
    title: str
    from_: int = Field(alias="from")
    to: int
    inExam: bool


class MaterialUnit(BaseModel):
    id: UUIDString
    no: int
    title: str
    from_: int = Field(alias="from")
    to: int
    difficulty: Literal["low", "mid", "high"] | None
    inExam: bool
    quizless: bool
    evidence: list[str]
    concepts: list[UnitConcept]


class UnitsFile(BaseModel):
    id: UUIDString
    name: str
    pages: int | None


class UnitsStructure(BaseModel):
    subjectId: UUIDString
    materialId: UUIDString
    file: UnitsFile
    units: list[MaterialUnit]


class UnitsResponse(BaseModel):
    success: Literal[True] = True
    data: UnitsStructure


class ConceptEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: UUIDString | None = None
    title: str
    from_: int = Field(alias='from', ge=1)
    to: int = Field(ge=1)
    inExam: bool


class UnitEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: UUIDString | None = None
    no: int = Field(ge=1)
    title: str
    from_: int = Field(alias='from', ge=1)
    to: int = Field(ge=1)
    difficulty: Literal['low', 'mid', 'high'] | None = None
    inExam: bool
    concepts: list[ConceptEdit]


class UnitsEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    subjectId: UUIDString
    materialId: UUIDString
    units: list[UnitEdit]


class ConceptSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    conceptId: UUIDString
    inExam: bool


class UnitSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    unitId: UUIDString
    inExam: bool | None = None
    concepts: list[ConceptSelection] = Field(default_factory=list)


class UnitsApproval(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    previewId: UUIDString
