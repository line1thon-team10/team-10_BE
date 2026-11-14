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


def page_count(spans: Iterable[PageSpan]) -> int:
    """Count unique inclusive pages, counting the same page in different PDFs twice."""
    return sum(span.end - span.start + 1 for span in normalize(spans))


def subtract(spans: Iterable[PageSpan], removed: Iterable[PageSpan]) -> tuple[PageSpan, ...]:
    """Return pages in spans but not removed, preserving holes and PDF identity."""
    sources = normalize(spans)
    removals = sources if spans is removed else normalize(removed)
    remaining: list[PageSpan] = []
    removal_index = 0

    for source in sources:
        cursor = source.start
        while removal_index < len(removals):
            removal = removals[removal_index]
            if removal.material_id < source.material_id or (
                removal.material_id == source.material_id and removal.end < cursor
            ):
                removal_index += 1
                continue
            if removal.material_id > source.material_id or removal.start > source.end:
                break
            if removal.start > cursor:
                remaining.append(PageSpan(source.material_id, cursor, removal.start - 1))
            cursor = max(cursor, removal.end + 1)
            if cursor > source.end:
                # This removal may also overlap the next source interval.
                break
            removal_index += 1
        if cursor <= source.end:
            remaining.append(PageSpan(source.material_id, cursor, source.end))

    return tuple(remaining)


def intersect(left: Iterable[PageSpan], right: Iterable[PageSpan]) -> tuple[PageSpan, ...]:
    """Return pages shared by both inputs, only within the same material."""
    left_spans = normalize(left)
    right_spans = left_spans if left is right else normalize(right)
    common: list[PageSpan] = []
    left_index = right_index = 0

    while left_index < len(left_spans) and right_index < len(right_spans):
        first = left_spans[left_index]
        second = right_spans[right_index]
        if first.material_id < second.material_id:
            left_index += 1
            continue
        if first.material_id > second.material_id:
            right_index += 1
            continue

        start = max(first.start, second.start)
        end = min(first.end, second.end)
        if start <= end:
            common.append(PageSpan(first.material_id, start, end))
        if first.end <= second.end:
            left_index += 1
        if second.end <= first.end:
            right_index += 1

    return tuple(common)


def covers(required: Iterable[PageSpan], completed: Iterable[PageSpan]) -> bool:
    """Whether every required page is completed, including all gaps and materials.

    Empty requirements are covered. Completion of an unpaged task remains a
    separate service decision based on its recorded result.
    """
    return not subtract(required, completed)
