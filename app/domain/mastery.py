"""Exact mastery arithmetic over immutable, finalized attempt facts."""
from dataclasses import dataclass
from collections import defaultdict
from datetime import date, datetime, timezone
from fractions import Fraction
from uuid import UUID


WEIGHTS = {"low": Fraction(1), "mid": Fraction(3, 2), "high": Fraction(2)}
QUIZ_KINDS = frozenset(("daily", "more", "custom", "retry"))


def _uuid(value, name):
    if type(value) is not str:
        raise ValueError(f"{name} must be a UUID string")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID string") from exc


def _day(value):
    if type(value) is not date:
        raise ValueError("A calendar date is required")
    return value


def _instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ConfirmedAttempt:
    """The caller must establish finalization, ownership and immutable metadata."""
    attempt_id: str
    quiz_item_id: str
    question_id: str
    material_id: str
    unit_id: str
    concept_id: str | None
    answered_date: date
    answered_at: datetime
    finalized_at: datetime
    difficulty: str
    confidence: str
    correct: bool
    quiz_kind: str

    def __post_init__(self):
        for field in ("attempt_id", "quiz_item_id", "question_id", "material_id", "unit_id"):
            object.__setattr__(self, field, _uuid(getattr(self, field), field))
        if self.concept_id is not None:
            object.__setattr__(self, "concept_id", _uuid(self.concept_id, "concept_id"))
        _day(self.answered_date)
        object.__setattr__(self, "answered_at", _instant(self.answered_at))
        object.__setattr__(self, "finalized_at", _instant(self.finalized_at))
        if self.finalized_at < self.answered_at:
            raise ValueError("A result cannot be finalized before its answer")
        if self.difficulty not in WEIGHTS or self.confidence not in ("sure", "unsure") or self.quiz_kind not in QUIZ_KINDS:
            raise ValueError("Invalid finalized attempt metadata")
        if type(self.correct) is not bool:
            raise ValueError("Correctness must be a confirmed boolean")

    @property
    def point(self):
        return Fraction(0) if not self.correct else Fraction(1) if self.confidence == "sure" else Fraction(1, 2)

    @property
    def weight(self):
        return WEIGHTS[self.difficulty]


def ordered_attempts(attempts) -> tuple[ConfirmedAttempt, ...]:
    """Deduplicate identical facts; reject conflicting identities instead of guessing."""
    unique, items, questions, units = {}, {}, {}, {}
    for fact in attempts:
        if not isinstance(fact, ConfirmedAttempt):
            raise ValueError("Only ConfirmedAttempt facts are accepted")
        previous = unique.get(fact.attempt_id)
        if previous is not None:
            if previous != fact:
                raise ValueError("Conflicting facts have the same attempt ID")
            continue
        if fact.quiz_item_id in items:
            raise ValueError("One finalized quiz item cannot be counted twice")
        metadata = (fact.material_id, fact.unit_id, fact.concept_id, fact.difficulty)
        if fact.question_id in questions and questions[fact.question_id] != metadata:
            raise ValueError("Question identity has conflicting unit or scoring metadata")
        if fact.unit_id in units and units[fact.unit_id] != fact.material_id:
            raise ValueError("Unit identity cannot refer to different materials")
        unique[fact.attempt_id], items[fact.quiz_item_id] = fact, fact.attempt_id
        questions[fact.question_id], units[fact.unit_id] = metadata, fact.material_id
    return tuple(sorted(unique.values(), key=lambda fact: (fact.answered_date, fact.answered_at, fact.attempt_id)))


@dataclass(frozen=True, slots=True)
class RawScore:
    value: Fraction
    all_rate: Fraction
    recent_rate: Fraction
    alpha: Fraction
    total_count: int
    recent_count: int


def _weighted_score(ordered) -> RawScore | None:
    if not ordered:
        return None
    def rate(facts):
        return sum((fact.point * fact.weight for fact in facts), Fraction()) / sum((fact.weight for fact in facts), Fraction())
    recent = ordered[-20:]
    alpha = Fraction(1, 2) if len(recent) < 10 else Fraction(2, 5) if len(recent) < 20 else Fraction(3, 10)
    all_rate, recent_rate = rate(ordered), rate(recent)
    return RawScore(100 * (alpha * all_rate + (1 - alpha) * recent_rate), all_rate, recent_rate,
                    alpha, len(ordered), len(recent))


def weighted_score(attempts) -> RawScore | None:
    """Return the uncapped 0..100 score; no result is different from a zero score."""
    facts = ordered_attempts(attempts)
    if len({fact.unit_id for fact in facts}) > 1:
        raise ValueError("A unit score cannot mix different units")
    return _weighted_score(facts)


def half_up(value: Fraction) -> int:
    if not isinstance(value, Fraction) or value < 0:
        raise ValueError("A nonnegative exact Fraction is required")
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def grade(score: int) -> str:
    if type(score) is not int or not 0 <= score <= 99:
        raise ValueError("A displayed integer score in 0..99 is required")
    return "risk" if score < 50 else "shaky" if score < 80 else "stable"


@dataclass(frozen=True, slots=True)
class UnitScope:
    material_id: str
    unit_id: str
    revision: str

    def __post_init__(self):
        object.__setattr__(self, "material_id", _uuid(self.material_id, "material_id"))
        object.__setattr__(self, "unit_id", _uuid(self.unit_id, "unit_id"))
        if type(self.revision) is not str or not self.revision.strip():
            raise ValueError("A current scope revision is required")


@dataclass(frozen=True, slots=True)
class DayEligibility:
    """S-03 supplies verified, current-scope evidence; this class does not create it."""
    scope: UnitScope
    day: date
    full_relearned: bool = False
    custom_confirmed: bool = False
    supplement_completed: bool = False

    def __post_init__(self):
        if not isinstance(self.scope, UnitScope):
            raise ValueError("A current unit scope is required")
        _day(self.day)
        if any(type(value) is not bool for value in (self.full_relearned, self.custom_confirmed, self.supplement_completed)):
            raise ValueError("Eligibility must be established boolean facts")


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    """An actually finalized set result below 80, identified by its stored history."""
    material_id: str
    unit_id: str
    recorded_date: date
    displayed_score: int
    history_id: str
    recorded_at: datetime

    def __post_init__(self):
        for field in ("material_id", "unit_id", "history_id"):
            object.__setattr__(self, field, _uuid(getattr(self, field), field))
        _day(self.recorded_date)
        object.__setattr__(self, "recorded_at", _instant(self.recorded_at))
        if type(self.displayed_score) is not int or not 0 <= self.displayed_score < 80:
            raise ValueError("A recovery entry must be an observed integer score below 80")


@dataclass(frozen=True, slots=True)
class DayScore:
    material_id: str
    unit_id: str
    day: date
    raw_score: Fraction
    capped_score: Fraction
    score: int
    grade: str
    previous_score: int | None
    recovery_mode: bool
    recovery_started_date: date | None
    recovery_result_dates: tuple[date, ...]
    daily_increase_limit: int | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConceptErrors:
    material_id: str
    unit_id: str
    concept_id: str | None
    wrong_count: int

    @property
    def is_weak(self):
        return self.wrong_count >= 2


@dataclass(frozen=True, slots=True)
class MasteryResult:
    history: tuple[DayScore, ...]
    units: tuple[DayScore, ...]
    concept_errors: tuple[ConceptErrors, ...]

    def latest(self, unit_id):
        return next((row for row in self.units if row.unit_id == unit_id), None)


def calculate_mastery(attempts, *, recovery_entries=(), current_scopes=(), eligibility=()) -> MasteryResult:
    """Replay one daily aggregate from its prior-day score, retaining real entry events.

    Reordering inputs and repeating identical facts cannot grant points. Different
    finalized set histories remain meaningful: a real first-day 70 anchors that
    day's later recovery at 75, even if its eventual raw aggregate reaches 90.
    """
    facts = ordered_attempts(attempts)
    by_unit, material_of = defaultdict(list), {}
    for fact in facts:
        by_unit[fact.unit_id].append(fact)
        material_of[fact.unit_id] = fact.material_id
    scopes = {}
    for scope in current_scopes:
        if not isinstance(scope, UnitScope):
            raise ValueError("UnitScope values are required")
        if scope.unit_id in scopes and scopes[scope.unit_id] != scope:
            raise ValueError("A unit has conflicting current scope revisions")
        if scope.unit_id in material_of and material_of[scope.unit_id] != scope.material_id:
            raise ValueError("The scope belongs to another material")
        scopes[scope.unit_id] = scope
    eligible = {}
    for evidence in eligibility:
        if not isinstance(evidence, DayEligibility):
            raise ValueError("Verified DayEligibility values are required")
        key = (evidence.scope, evidence.day)
        if key in eligible and eligible[key] != evidence:
            raise ValueError("Conflicting daily eligibility snapshots")
        eligible[key] = evidence
    entries, first_entries = {}, {}
    for entry in recovery_entries:
        if not isinstance(entry, RecoveryEntry):
            raise ValueError("Observed RecoveryEntry values are required")
        if entry.history_id in entries and entries[entry.history_id] != entry:
            raise ValueError("Conflicting facts have the same history ID")
        if entry.unit_id in material_of and material_of[entry.unit_id] != entry.material_id:
            raise ValueError("The recovery entry belongs to another material")
        entries[entry.history_id] = entry
    for entry in sorted(entries.values(), key=lambda row: (row.recorded_date, row.recorded_at, row.history_id)):
        first_entries.setdefault(entry.unit_id, entry)

    history, latest = [], []
    for unit_id in sorted(by_unit):
        scope, entry = scopes.get(unit_id), first_entries.get(unit_id)
        per_day, accumulated, previous, recovery_start, result_days = defaultdict(list), [], None, None, []
        for fact in by_unit[unit_id]:
            per_day[fact.answered_date].append(fact)
        for day in sorted(per_day):
            accumulated.extend(per_day[day])
            raw = _weighted_score(accumulated).value
            candidate, reasons = min(raw, Fraction(99)), []
            if raw > 99:
                reasons.append("maximum_99")
            if entry is not None and entry.recorded_date <= day:
                recovery_start = min(recovery_start, entry.recorded_date) if recovery_start else entry.recorded_date
            observed_recovery = recovery_start is not None
            evidence = eligible.get((scope, day)) if scope else None
            has_custom = any(fact.quiz_kind == "custom" for fact in per_day[day])
            boost = bool(evidence and evidence.full_relearned and evidence.custom_confirmed and has_custom)
            limit = 15 if boost else 5
            anchor = previous.capped_score if previous else (
                Fraction(entry.displayed_score) if entry is not None and entry.recorded_date == day else None)
            if observed_recovery and anchor is not None and candidate > anchor + limit:
                candidate = anchor + limit
                reasons.append("daily_recovery_limit")
            previous_grade = previous.grade if previous else grade(entry.displayed_score) if entry and entry.recorded_date == day else None
            if previous_grade == "risk" and half_up(candidate) >= 80:
                candidate = Fraction(79)
                reasons.append("one_grade_per_day")
            if recovery_start is None and half_up(candidate) < 80:
                recovery_start = day
            if recovery_start is not None:
                result_days = [result_day for result_day in per_day if recovery_start <= result_day <= day]
                supplement = bool(scope and any(
                    snapshot.scope == scope and recovery_start <= snapshot.day <= day and snapshot.supplement_completed
                    for snapshot in eligible.values()))
                if half_up(candidate) >= 80 and (not supplement or len(result_days) < 2):
                    candidate = Fraction(79)
                    reasons.append("recovery_stable_requires_supplement_and_two_dates")
            score = half_up(candidate)
            previous = DayScore(material_of[unit_id], unit_id, day, raw, candidate, score, grade(score),
                previous.score if previous else None, recovery_start is not None, recovery_start,
                tuple(sorted(result_days)), limit if recovery_start is not None else None, tuple(reasons))
            history.append(previous)
        latest.append(previous)

    wrong = defaultdict(int)
    for fact in facts:
        if not fact.correct:
            wrong[(fact.material_id, fact.unit_id, fact.concept_id)] += 1
    errors = tuple(ConceptErrors(material, unit, concept, count) for (material, unit, concept), count
                   in sorted(wrong.items(), key=lambda row: (row[0][0], row[0][1], row[0][2] or "")))
    return MasteryResult(tuple(sorted(history, key=lambda row: (row.day, row.unit_id))), tuple(latest), errors)
