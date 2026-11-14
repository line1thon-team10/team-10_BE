"""Shared task range projection; callers supply this task's current snapshot."""
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.ranges import PageSpan, intersect, page_count


def unit_no(code: str | None) -> int | None:
    match = re.search(r"(\d+)$", code or "")
    return int(match.group(1)) if match else None


@dataclass(frozen=True, slots=True)
class TaskRangeView:
    id: str
    material_id: str
    unit_id: str
    concept_id: str | None
    start: int
    end: int
    done_to: int | None

    def as_dict(self) -> dict:
        return {"id": self.id, "materialId": self.material_id, "unitId": self.unit_id,
                "conceptId": self.concept_id, "from": self.start, "to": self.end,
                "doneTo": self.done_to}


@dataclass(frozen=True, slots=True)
class TaskView:
    ranges: tuple[TaskRangeView, ...]
    pages: int
    done_pages: int
    first: int | None
    last: int | None
    done_to: int | None
    unit_no: int | None

    @classmethod
    def from_ranges(cls, ranges: Iterable[Mapping[str, Any]],
                    completed_ranges: Iterable[Mapping[str, Any]] = (), *,
                    unit_code: str | None = None) -> "TaskView":
        """Keep placement order, intersect by unit, then count by material.

        Null bounds represent an unpaged obligation, not a completed page.
        The caller must restrict completed_ranges to the latest record of this
        task; another task or learning pass is never a completion source here.
        """
        raw_ranges = tuple(ranges)
        by_unit = defaultdict(list)
        for item in completed_ranges:
            if item["start_page"] is not None and item["end_page"] is not None:
                by_unit[item["unit_id"]].append(
                    PageSpan(item["unit_id"], item["start_page"], item["end_page"]))

        views, planned, completed = [], [], []
        for item in raw_ranges:
            if item["start_page"] is None or item["end_page"] is None:
                continue
            span = PageSpan(item["material_id"], item["start_page"], item["end_page"])
            planned.append(span)
            matches = intersect([PageSpan(item["unit_id"], span.start, span.end)], by_unit[item["unit_id"]])
            completed.extend(PageSpan(span.material_id, match.start, match.end) for match in matches)
            # A later completed island must not imply that its missing prefix is done.
            done_to = matches[0].end if matches and matches[0].start == span.start else None
            views.append(TaskRangeView(item["id"], span.material_id, item["unit_id"], item["concept_id"],
                                       span.start, span.end, done_to))

        single = views[0] if len(raw_ranges) == 1 and views else None
        return cls(tuple(views), page_count(planned), page_count(completed),
                   single.start if single else None, single.end if single else None,
                   single.done_to if single else None, unit_no(unit_code) if single else None)

    def as_dict(self) -> dict:
        return {"ranges": [item.as_dict() for item in self.ranges], "pages": self.pages,
                "donePages": self.done_pages, "from": self.first, "to": self.last,
                "doneTo": self.done_to, "unitNo": self.unit_no}
