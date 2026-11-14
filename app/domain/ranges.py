"""Inclusive page interval algebra, keeping each PDF's identity separate."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageSpan:
    """A nonempty inclusive interval in one material; no page coercion is allowed."""

    material_id: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.material_id, str) or not self.material_id.strip():
            raise ValueError("material_id must be a nonempty string")
        if type(self.start) is not int or type(self.end) is not int:
            raise ValueError("page bounds must be integers, excluding booleans")
        if not 1 <= self.start <= self.end:
            raise ValueError("page bounds must satisfy 1 <= start <= end")
