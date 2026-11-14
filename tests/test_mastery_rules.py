from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from uuid import UUID

import pytest

from app.domain.mastery import ConfirmedAttempt, grade, half_up, ordered_attempts, weighted_score


def uid(number):
    return str(UUID(int=number))


DAY = date(2026, 11, 9)
AT = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def fact(number, *, day=0, correct=True, confidence="sure", difficulty="low", **changes):
    fields = dict(attempt_id=uid(1000 + number), quiz_item_id=uid(2000 + number), question_id=uid(3000 + number),
                  material_id=uid(1), unit_id=uid(2), concept_id=uid(3), answered_date=DAY + timedelta(days=day),
                  answered_at=AT + timedelta(days=day, seconds=number), finalized_at=AT + timedelta(days=day, seconds=number + 1),
                  difficulty=difficulty, confidence=confidence, correct=correct, quiz_kind="daily")
    fields.update(changes)
    return ConfirmedAttempt(**fields)


def test_no_facts_is_different_from_a_confirmed_zero_and_facts_are_immutable():
    assert weighted_score([]) is None
    attempt = fact(1, correct=False)
    assert weighted_score([attempt]).value == 0
    with pytest.raises(FrozenInstanceError):
        attempt.correct = True


@pytest.mark.parametrize("confidence,correct,expected", [("sure", True, 100), ("unsure", True, 50),
                                                        ("sure", False, 0), ("unsure", False, 0)])
def test_confidence_and_correctness_points(confidence, correct, expected):
    assert weighted_score([fact(1, confidence=confidence, correct=correct)]).value == expected


def test_difficulty_is_an_exact_weight_without_rounding_intermediate_rates():
    score = weighted_score([fact(1, difficulty="low"), fact(2, difficulty="mid", confidence="unsure"),
                            fact(3, difficulty="high", correct=False)])
    assert score.all_rate == score.recent_rate == Fraction(7, 18)
    assert score.value == Fraction(350, 9) and half_up(score.value) == 39


@pytest.mark.parametrize("count,alpha", [(9, Fraction(1, 2)), (10, Fraction(2, 5)), (19, Fraction(2, 5)),
                                        (20, Fraction(3, 10)), (21, Fraction(3, 10))])
def test_recent_window_and_alpha_boundaries(count, alpha):
    score = weighted_score([fact(n, correct=n > 1) for n in range(1, count + 1)])
    assert score.total_count == count and score.recent_count == min(count, 20) and score.alpha == alpha
    all_rate = Fraction(count - 1, count)
    recent_rate = Fraction(1) if count == 21 else all_rate
    assert score.value == 100 * (alpha * all_rate + (1 - alpha) * recent_rate)


@pytest.mark.parametrize("value,rounded,level", [(Fraction(49), 49, "risk"), (Fraction(99, 2), 50, "shaky"),
    (Fraction(50), 50, "shaky"), (Fraction(79), 79, "shaky"), (Fraction(159, 2), 80, "stable"),
    (Fraction(80), 80, "stable"), (Fraction(99), 99, "stable")])
def test_half_up_and_grade_thresholds(value, rounded, level):
    assert half_up(value) == rounded and grade(rounded) == level


def test_exact_fraction_does_not_round_an_under_half_value_up():
    assert half_up(Fraction(50) - Fraction(1, 2) - Fraction(1, 10 ** 50)) == 49
    assert half_up(Fraction(99, 2)) == 50


def test_attempt_order_uses_date_instant_then_id_and_deduplicates():
    first, second = fact(1), fact(2)
    same_instant = replace(second, answered_at=first.answered_at, finalized_at=first.finalized_at)
    # answered_date is authoritative, including virtual demo dates.
    earlier_date = replace(fact(3), answered_date=DAY - timedelta(days=1))
    assert ordered_attempts([same_instant, first, earlier_date, first]) == (earlier_date, first, same_instant)
    assert weighted_score([first, first]) == weighted_score([first])
    shifted = replace(first, answered_at=first.answered_at.astimezone(timezone(timedelta(hours=9))))
    assert shifted == first


def test_conflicting_attempt_item_question_or_material_identity_is_rejected():
    first = fact(1)
    conflicts = [replace(first, correct=False), replace(fact(2), quiz_item_id=first.quiz_item_id),
                 replace(fact(2), question_id=first.question_id, concept_id=None), replace(fact(2), material_id=uid(50))]
    for second in conflicts:
        with pytest.raises(ValueError):
            ordered_attempts([first, second])
    with pytest.raises(ValueError):
        weighted_score([first, replace(fact(2), unit_id=uid(50))])


@pytest.mark.parametrize("changes", [{"correct": None}, {"correct": 1}, {"confidence": "확실"}, {"difficulty": "unknown"},
    {"quiz_kind": "ready"}, {"finalized_at": None}, {"answered_at": AT.replace(tzinfo=None)},
    {"finalized_at": AT - timedelta(days=1)}, {"answered_date": AT}, {"unit_id": "same-code"}, {"concept_id": ""}])
def test_unconfirmed_invalid_or_ambiguous_facts_are_rejected(changes):
    with pytest.raises(ValueError):
        fact(1, **changes)
