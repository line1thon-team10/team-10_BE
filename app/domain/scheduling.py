"""Immutable, local-time inputs and exact arithmetic for allocation-v1."""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
import math
from typing import Literal

from app.domain.ranges import PageSpan, normalize, page_count

POLICY_VERSION = "allocation-v1"
DAY_START, DAY_END = Fraction(8 * 60), Fraction(23 * 60)
MIN_TASK, MAX_TASK = Fraction(15), Fraction(60)
MULTIPLIERS = {"low": Fraction(1), "mid": Fraction(6, 5), "high": Fraction(3, 2), None: Fraction(6, 5)}
Number = int | float | Decimal | Fraction


def exact(value: Number) -> Fraction:
    """Accept finite numbers without losing Decimal boundaries through float."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, Fraction)):
        raise ValueError("A finite numeric value is required")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("A finite numeric value is required")
        return Fraction(str(value))
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("A finite numeric value is required")
    return Fraction(value)


def _id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A nonempty identifier is required")


def _date(value: date) -> None:
    if type(value) is not date:
        raise ValueError("A local date is required")


def _tuple(values, expected_type):
    if isinstance(values, (str, bytes)):
        raise ValueError("An iterable of typed values is required")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ValueError("An iterable of typed values is required") from exc
    if any(not isinstance(value, expected_type) for value in result):
        raise ValueError("An iterable of typed values is required")
    return result


@dataclass(frozen=True, slots=True)
class Subject:
    id: str
    exam_date: date | None
    target_score: Number | None
    exam_confirmed: bool = False
    goal_confirmed: bool = False
    minutes_per_page: Number = Fraction(5, 2)
    target_effective_date: date | None = None
    previous_target_score: Number | None = None

    def __post_init__(self):
        _id(self.id)
        if self.exam_date is not None:
            _date(self.exam_date)
        if type(self.exam_confirmed) is not bool or type(self.goal_confirmed) is not bool:
            raise ValueError("Confirmation values must be boolean")
        if self.target_score is not None:
            target = exact(self.target_score)
            if not 0 <= target <= 100:
                raise ValueError("Target must be in 0..100")
            object.__setattr__(self, "target_score", target)
        if (self.target_effective_date is None) != (self.previous_target_score is None):
            raise ValueError("A scheduled target needs both an effective date and the previous target")
        if self.target_effective_date is not None:
            _date(self.target_effective_date)
            previous = exact(self.previous_target_score)
            if not 0 <= previous <= 100:
                raise ValueError("Previous target must be in 0..100")
            object.__setattr__(self, "previous_target_score", previous)
        speed = exact(self.minutes_per_page)
        if speed <= 0:
            raise ValueError("Baseline minutes per page must be positive")
        object.__setattr__(self, "minutes_per_page", speed)

    def target_on(self, day: date):
        _date(day)
        if self.target_effective_date is not None and day < self.target_effective_date:
            return self.previous_target_score
        return self.target_score


@dataclass(frozen=True, slots=True)
class UnitScope:
    id: str
    subject_id: str
    material_id: str
    sort_order: int
    ranges: tuple[PageSpan, ...]
    difficulty: Literal["low", "mid", "high"] | None = None
    mastery_score: Number | None = None

    def __post_init__(self):
        for value in (self.id, self.subject_id, self.material_id):
            _id(value)
        if type(self.sort_order) is not int or self.sort_order < 0:
            raise ValueError("Unit sort order must be a nonnegative integer")
        if self.difficulty not in ("low", "mid", "high", None):
            raise ValueError("Unknown difficulty")
        ranges = normalize(self.ranges)
        if any(span.material_id != self.material_id for span in ranges):
            raise ValueError("A unit belongs to one material")
        object.__setattr__(self, "ranges", ranges)
        if self.mastery_score is not None:
            mastery = exact(self.mastery_score)
            if not 0 <= mastery <= 100:
                raise ValueError("Mastery must be in 0..100")
            object.__setattr__(self, "mastery_score", mastery)


@dataclass(frozen=True, slots=True)
class ScopePart:
    unit_id: str
    span: PageSpan

    def __post_init__(self):
        _id(self.unit_id)
        if not isinstance(self.span, PageSpan):
            raise ValueError("A page span is required")


@dataclass(frozen=True, slots=True)
class WorkRequirement:
    """A separate review/remedial obligation, even when pages were studied."""
    id: str
    subject_id: str
    kind: Literal["remedial", "review"]
    parts: tuple[ScopePart, ...]
    not_before: date | None = None
    indivisible: bool = False

    def __post_init__(self):
        _id(self.id)
        _id(self.subject_id)
        if self.kind not in ("remedial", "review"):
            raise ValueError("Study requirements come from current unit scopes")
        parts = _tuple(self.parts, ScopePart)
        if not parts:
            raise ValueError("A page-backed obligation must contain ranges")
        object.__setattr__(self, "parts", parts)
        if type(self.indivisible) is not bool:
            raise ValueError("Indivisible must be boolean")
        if self.not_before is not None:
            _date(self.not_before)


@dataclass(frozen=True, slots=True)
class FixedRange:
    unit_id: str
    span: PageSpan
    requirement_id: str | None = None

    def __post_init__(self):
        _id(self.unit_id)
        if not isinstance(self.span, PageSpan):
            raise ValueError("A page span is required")
        if self.requirement_id is not None:
            _id(self.requirement_id)


@dataclass(frozen=True, slots=True)
class BusyInterval:
    day: date
    start: Number
    end: Number

    def __post_init__(self):
        _date(self.day)
        start, end = exact(self.start), exact(self.end)
        if not 0 <= start < end <= 24 * 60:
            raise ValueError("Intervals must fit a local day; split overnight events")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class FixedTask:
    id: str
    subject_id: str
    kind: Literal["study", "remedial", "review"]
    state: Literal["started", "completed"]
    day: date
    start: Number
    end: Number
    ranges: tuple[FixedRange, ...] = ()

    def __post_init__(self):
        _id(self.id)
        _id(self.subject_id)
        if self.kind not in ("study", "remedial", "review") or self.state not in ("started", "completed"):
            raise ValueError("Only started/completed tasks are fixed")
        interval = BusyInterval(self.day, self.start, self.end)
        object.__setattr__(self, "start", interval.start)
        object.__setattr__(self, "end", interval.end)
        object.__setattr__(self, "ranges", _tuple(self.ranges, FixedRange))

    @property
    def minutes(self) -> Fraction:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Slot:
    day: date
    start: Fraction
    end: Fraction

    @property
    def minutes(self) -> Fraction:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class StudyWindow:
    """Constrain selected new-study pages without restricting adjacent old work."""
    id: str
    ranges: tuple[PageSpan, ...]
    dates: tuple[date, ...]
    max_minutes: Number | None = None

    def __post_init__(self):
        _id(self.id)
        object.__setattr__(self, "ranges", normalize(self.ranges))
        dates = tuple(self.dates)
        for day in dates:
            _date(day)
        object.__setattr__(self, "dates", tuple(sorted(set(dates))))
        if self.max_minutes is not None:
            cap = exact(self.max_minutes)
            if cap <= 0:
                raise ValueError("Daily study limit must be positive")
            object.__setattr__(self, "max_minutes", cap)


@dataclass(frozen=True, slots=True)
class AllocationInput:
    start_date: date
    subjects: tuple[Subject, ...]
    units: tuple[UnitScope, ...]
    busy: tuple[BusyInterval, ...] = ()
    fixed: tuple[FixedTask, ...] = ()
    requirements: tuple[WorkRequirement, ...] = ()
    study_windows: tuple[StudyWindow, ...] = ()

    def __post_init__(self):
        _date(self.start_date)
        for name, item_type in (("subjects", Subject), ("units", UnitScope), ("busy", BusyInterval),
                                ("fixed", FixedTask), ("requirements", WorkRequirement), ("study_windows", StudyWindow)):
            object.__setattr__(self, name, _tuple(getattr(self, name), item_type))


@dataclass(frozen=True, slots=True)
class AllocatedPart:
    requirement_id: str
    unit_id: str
    span: PageSpan
    baseline_minutes_per_page: Fraction
    difficulty_multiplier: Fraction

    @property
    def minutes(self) -> Fraction:
        return (self.span.end - self.span.start + 1) * self.baseline_minutes_per_page * self.difficulty_multiplier


@dataclass(frozen=True, slots=True)
class Placement:
    id: str
    subject_id: str
    kind: Literal["study", "remedial", "review"]
    day: date
    start: Fraction
    end: Fraction
    parts: tuple[AllocatedPart, ...]

    @property
    def minutes(self) -> Fraction:
        return self.end - self.start

    @property
    def display_minutes(self) -> int:
        return half_up(self.minutes)


@dataclass(frozen=True, slots=True)
class Unplaced:
    requirement_id: str
    subject_id: str
    kind: str
    parts: tuple[ScopePart, ...]


@dataclass(frozen=True, slots=True)
class AllocationReason:
    code: str
    message: str
    day: date | None = None
    deviation_minutes: Fraction | None = None


@dataclass(frozen=True, slots=True)
class AllocationResult:
    feasible: bool
    placements: tuple[Placement, ...]
    fixed: tuple[FixedTask, ...]
    unplaced: tuple[Unplaced, ...]
    reasons: tuple[AllocationReason, ...]
    policy_version: str = POLICY_VERSION


class SearchLimit(RuntimeError):
    """No feasibility/optimality claim can be made after a resource limit."""
    def __init__(self, limit: int, explored: int):
        self.limit, self.explored = limit, explored
        super().__init__("배치 탐색 한도에 도달했어요. 불가능 판정이 아니며 더 큰 탐색 예산이 필요해요")


def half_up(minutes: Number) -> int:
    value = exact(minutes)
    if value < 0:
        raise ValueError("Minutes cannot be negative")
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def free_slots(day: date, busy: Iterable[BusyInterval] = (), fixed: Iterable[FixedTask] = ()) -> tuple[Slot, ...]:
    """Subtract interval union; separate short gaps remain separate slots."""
    _date(day)
    blocked = sorted((max(DAY_START, item.start), min(DAY_END, item.end))
                     for item in (*_tuple(busy, BusyInterval), *_tuple(fixed, FixedTask))
                     if item.day == day and item.end > DAY_START and item.start < DAY_END)
    cursor, slots = DAY_START, []
    for start, end in blocked:
        if start > cursor:
            slots.append(Slot(day, cursor, start))
        cursor = max(cursor, end)
    if cursor < DAY_END:
        slots.append(Slot(day, cursor, DAY_END))
    return tuple(slots)


def take_pages(spans: Iterable[PageSpan], count: int) -> tuple[tuple[PageSpan, ...], tuple[PageSpan, ...]]:
    ranges = normalize(spans)
    if type(count) is not int or not 0 <= count <= page_count(ranges):
        raise ValueError("Page count must be an integer within the available union")
    taken, rest = [], []
    for span in ranges:
        length = span.end - span.start + 1
        used = min(count, length)
        if used:
            taken.append(PageSpan(span.material_id, span.start, span.start + used - 1))
        if used < length:
            rest.append(PageSpan(span.material_id, span.start + used, span.end))
        count -= used
    return tuple(taken), tuple(rest)


def mastery_ratio(subject_id: str, units: Iterable[UnitScope]) -> Fraction:
    measured = [(unit.mastery_score, page_count(unit.ranges)) for unit in units
                if unit.subject_id == subject_id and unit.mastery_score is not None and unit.ranges]
    pages = sum(count for _, count in measured)
    return sum((score * count for score, count in measured), Fraction()) / (100 * pages) if pages else Fraction()


def allocation_score(subject: Subject, day: date, remaining_pages: int, mastery: Fraction) -> Fraction:
    if type(remaining_pages) is not int or remaining_pages < 0:
        raise ValueError("Remaining pages must be a nonnegative integer")
    _date(day)
    if subject.exam_date is None or subject.target_score is None or day >= subject.exam_date:
        return Fraction()
    return subject.target_on(day) / 100 * Fraction(1, (subject.exam_date - day).days) * remaining_pages * (1 - mastery)


def reservations(subjects: Iterable[Subject], start_date: date) -> tuple[tuple[date, tuple[str, ...]], ...]:
    """Later exam groups win a collision on the same D-1/D-2 date."""
    _date(start_date)
    groups = {}
    for subject in subjects:
        if subject.exam_date is not None:
            groups.setdefault(subject.exam_date, []).append(subject.id)
    exams, claimed, result = set(groups), set(), []
    for exam in sorted(groups, reverse=True):
        if exam <= start_date:
            continue
        day = exam - timedelta(days=1)
        if day in exams:
            if day <= start_date:
                continue
            day -= timedelta(days=1)
        if day >= start_date and day not in claimed:
            claimed.add(day)
            result.append((day, tuple(sorted(groups[exam]))))
    return tuple(result)


def group_targets(capacity: Number, focus_work: Number, other_work: Number) -> tuple[Fraction, Fraction]:
    """Cap the 80/20 free-time target and transfer unused capacity."""
    capacity, focus, other = exact(capacity), exact(focus_work), exact(other_work)
    if min(capacity, focus, other) < 0:
        raise ValueError("Capacity and work cannot be negative")
    first, second = min(focus, capacity * Fraction(4, 5)), min(other, capacity / 5)
    spare = capacity - first - second
    extra = min(focus - first, spare)
    first += extra
    second += min(other - second, spare - extra)
    return first, second
