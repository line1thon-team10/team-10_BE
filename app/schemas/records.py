"""Strict record commands; omitted optional fields are distinct from null."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _uuid_string(value):
    if type(value) is not str:
        raise ValueError("IDs must be UUID strings")
    return str(UUID(value))


UUIDString = Annotated[str, BeforeValidator(_uuid_string)]
PageNumber = Annotated[int, Field(strict=True, ge=1)]


class CompletedRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rangeId: UUIDString
    toPage: PageNumber


class RangeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    toPage: PageNumber | None = None
    completedRanges: list[CompletedRange] | None = None

    @model_validator(mode="after")
    def validate_range_inputs(self):
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError("Optional fields must be omitted rather than null")
        if self.toPage is not None and self.completedRanges is not None:
            raise ValueError("Use exactly one range representation")
        if self.completedRanges is not None:
            ids = [item.rangeId for item in self.completedRanges]
            if len(ids) != len(set(ids)):
                raise ValueError("Range IDs must be unique")
        return self


class RecordIn(RangeCommand):
    taskId: UUIDString
    result: Literal["done", "partial", "none"]
    sessionId: UUIDString | None = None

    @model_validator(mode="after")
    def validate_legacy_page(self):
        if self.toPage is not None and self.result != "partial":
            raise ValueError("toPage is only a compatibility input for partial records")
        return self
