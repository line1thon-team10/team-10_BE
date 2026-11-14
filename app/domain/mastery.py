"""Exact mastery arithmetic over immutable, finalized attempt facts."""
from dataclasses import dataclass
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
