"""Inclusive page interval algebra, keeping each PDF's identity separate."""

from collections.abc import Iterable
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


def normalize(spans: Iterable[PageSpan]) -> tuple[PageSpan, ...]:
    """Return the union sorted by material ID and start, merging adjacent pages.

    Inputs are consumed once, and invalid items raise ValueError. Work and memory
    depend on the number of intervals rather than the number of pages.
    """
    if isinstance(spans, (str, bytes, bytearray)):
        raise ValueError("spans must be an iterable of PageSpan values")
    try:
        iterator = iter(spans)
    except TypeError as exc:
        raise ValueError("spans must be an iterable of PageSpan values") from exc

    validated = []
    for span in iterator:
        if not isinstance(span, PageSpan):
            raise ValueError("each span must be a PageSpan value")
        validated.append(span)

    merged: list[PageSpan] = []
    for span in sorted(validated, key=lambda item: (item.material_id, item.start, item.end)):
        previous = merged[-1] if merged else None
        if previous is not None and previous.material_id == span.material_id and span.start <= previous.end + 1:
            if span.end > previous.end:
                merged[-1] = PageSpan(span.material_id, previous.start, span.end)
        else:
            merged.append(span)
    return tuple(merged)
