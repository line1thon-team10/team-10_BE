from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from uuid import UUID

import pytest

from app.domain.mastery import (
    ConfirmedAttempt, DayEligibility, RecoveryEntry, UnitScope, calculate_mastery,
    grade, half_up, ordered_attempts, weighted_score,
)


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


SCOPE = UnitScope(uid(1), uid(2), "scope-v1")


def batch(day, correct_count=20, *, offset=None, kind="daily"):
    start = day * 100 if offset is None else offset
    return [fact(start + n, day=day, correct=n <= correct_count, quiz_kind=kind) for n in range(1, 21)]


def entry(score=70, *, day=0, number=1, **changes):
    fields = dict(material_id=uid(1), unit_id=uid(2), recorded_date=DAY + timedelta(days=day),
                  displayed_score=score, history_id=uid(8000 + number), recorded_at=AT + timedelta(days=day, seconds=1000 + number))
    fields.update(changes)
    return RecoveryEntry(**fields)


def calculate(facts, **kwargs):
    return calculate_mastery(facts, current_scopes=[SCOPE], **kwargs)


def test_first_measurement_is_not_anchored_to_zero_and_raw_remains_uncapped():
    result = calculate(batch(0))
    row = result.latest(uid(2))
    assert (row.raw_score, row.capped_score, row.score, row.grade) == (100, 99, 99, "stable")
    assert not row.recovery_mode and row.previous_score is None
    assert row.daily_increase_limit is None
    assert calculate([]).history == () and calculate([]).units == ()


def test_recovery_groups_each_date_once_and_does_not_stack_five_point_increases():
    facts = batch(0, 14) + batch(1) + batch(1, offset=200)
    result = calculate(facts)
    assert [row.score for row in result.history] == [70, 75]
    assert result.units[0].raw_score > 95 and result.units[0].daily_increase_limit == 5
    assert calculate(list(reversed(facts)) + [facts[0]]) == result
    assert calculate(batch(0, 14) + batch(1)).units[0].score == 75


def test_observed_first_day_entry_survives_later_high_aggregate_and_anchors_at_its_score():
    facts = batch(0, 14) + batch(0, offset=20) + batch(0, offset=40)
    result = calculate(facts, recovery_entries=[entry()]).units[0]
    assert result.raw_score == 97 and result.score == 75 and result.recovery_mode
    assert result.recovery_result_dates == (DAY,)
    assert result.recovery_started_date == DAY
    # Without an actual entry, do not invent an earlier low set from a question prefix.
    unobserved = calculate(facts).units[0]
    assert unobserved.score == 97 and not unobserved.recovery_mode


def test_first_day_fifteen_bonus_still_cannot_skip_the_two_date_stable_requirement():
    facts = batch(0, 14) + batch(0, offset=20, kind="custom") + batch(0, offset=40)
    verified = DayEligibility(SCOPE, DAY, full_relearned=True, custom_confirmed=True, supplement_completed=True)
    row = calculate(facts, recovery_entries=[entry()], eligibility=[verified]).units[0]
    assert row.daily_increase_limit == 15 and row.score == 79
    assert row.recovery_result_dates == (DAY,)


@pytest.mark.parametrize("full,custom,kind,expected", [(False, False, "custom", 75), (True, False, "custom", 75),
    (False, True, "custom", 75), (True, True, "daily", 75), (True, True, "custom", 85)])
def test_fifteen_requires_full_relearning_and_confirmed_custom_in_the_same_scope_and_date(full, custom, kind, expected):
    evidence = DayEligibility(SCOPE, DAY + timedelta(days=1), full_relearned=full, custom_confirmed=custom, supplement_completed=True)
    row = calculate(batch(0, 14) + batch(1, kind=kind), eligibility=[evidence]).units[0]
    assert row.score == expected and row.daily_increase_limit == (15 if expected == 85 else 5)


@pytest.mark.parametrize("mismatch", ["day", "revision", "unit", "material"])
def test_other_date_revision_unit_or_material_evidence_never_grants_a_bonus(mismatch):
    evidence = DayEligibility(SCOPE, DAY + timedelta(days=1), True, True, True)
    if mismatch == "day":
        evidence = replace(evidence, day=DAY)
    else:
        scope = replace(SCOPE, **{ {"revision": "revision", "unit": "unit_id", "material": "material_id"}[mismatch]:
                                 "old" if mismatch == "revision" else uid(99)})
        evidence = replace(evidence, scope=scope)
    row = calculate(batch(0, 14) + batch(1, kind="custom"), eligibility=[evidence]).units[0]
    assert row.score == 75 and row.daily_increase_limit == 5


def test_recovery_entry_date_counts_and_prior_dates_do_not_count_toward_two_results():
    facts = batch(0) + batch(1) + batch(2)
    recovery = entry(score=79, day=1)
    done = DayEligibility(SCOPE, DAY + timedelta(days=1), supplement_completed=True)
    result = calculate(facts, recovery_entries=[recovery], eligibility=[done])
    assert [row.score for row in result.history] == [99, 79, 84]
    assert result.history[1].recovery_result_dates == (DAY + timedelta(days=1),)
    assert result.history[2].recovery_result_dates == (DAY + timedelta(days=1), DAY + timedelta(days=2))
    assert result.history[2].grade == "stable" and result.history[2].recovery_mode


def test_seventy_nine_entry_and_next_day_result_with_supplement_can_recover():
    facts = [fact(1, difficulty="high"), fact(2, difficulty="mid", confidence="unsure")] + batch(1)
    result = calculate(facts, recovery_entries=[entry(79)],
                       eligibility=[DayEligibility(SCOPE, DAY + timedelta(days=1), supplement_completed=True)])
    assert [row.score for row in result.history] == [79, 84]
    assert result.history[0].capped_score == Fraction(550, 7)
    assert result.history[1].capped_score == Fraction(585, 7)  # prior unrounded value + 5


def test_many_result_dates_without_verified_supplement_never_promote_recovery_to_stable():
    facts = batch(0, 14) + batch(1) + batch(2) + batch(3)
    result = calculate(facts)
    assert [row.score for row in result.history] == [70, 75, 79, 79]
    assert result.units[0].raw_score > 90 and result.units[0].grade == "shaky"


def test_a_supplement_before_recovery_entry_is_not_current_recovery_evidence():
    row = calculate(batch(0) + batch(1) + batch(2), recovery_entries=[entry(79, day=1)],
                    eligibility=[DayEligibility(SCOPE, DAY, supplement_completed=True)]).units[0]
    assert row.score == 79


def test_decreases_are_unrestricted_and_risk_cannot_jump_to_stable_in_a_day():
    facts = batch(0) + batch(1, 0)
    falling = calculate(facts)
    assert [row.score for row in falling.history] == [99, 15]
    evidence = DayEligibility(SCOPE, DAY + timedelta(days=2), True, True, True)
    recovered = calculate(facts + batch(2, kind="custom"), eligibility=[evidence]).units[0]
    assert recovered.raw_score > 80 and recovered.score == 30 and recovered.grade == "risk"


def test_history_entry_selection_is_stable_and_conflicting_history_ids_are_rejected():
    first, later = entry(70), entry(75, number=2)
    facts = batch(0)
    assert calculate(facts, recovery_entries=[later, first, first]) == calculate(facts, recovery_entries=[first, later])
    assert calculate(facts, recovery_entries=[later, first]).units[0].score == 75
    with pytest.raises(ValueError):
        calculate(facts, recovery_entries=[first, replace(first, displayed_score=65)])
    for invalid in (True, -1, 80, 79.1):
        with pytest.raises(ValueError):
            entry(invalid)


def test_conceptless_and_real_concept_errors_stay_separate_per_material_and_unit():
    facts = [fact(1, correct=False, concept_id=None), fact(2, correct=False), fact(3, correct=False),
             fact(4, correct=False, concept_id=None, material_id=uid(4), unit_id=uid(5))]
    result = calculate_mastery(facts)
    assert len(result.units) == 2
    assert [(row.unit_id, row.concept_id, row.wrong_count, row.is_weak) for row in result.concept_errors] == [
        (uid(2), None, 1, False), (uid(2), uid(3), 2, True), (uid(5), None, 1, False)]
