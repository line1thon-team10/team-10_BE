from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import permutations

import pytest

from app.domain.ranges import PageSpan, page_count
from app.domain.scheduling import (
    AllocationInput, BusyInterval, FixedTask, Subject, UnitScope, allocation_score, exact, free_slots,
    group_targets, half_up, mastery_ratio, reservations, take_pages,
)

TODAY = date(2026, 11, 14)


def subject(identifier="a", *, days=5, target=90, speed=Fraction(5, 2), **kwargs):
    return Subject(identifier, TODAY + timedelta(days=days), target, True, True, speed, **kwargs)


def unit(identifier="u", owner="a", *, material="pdf", pages=30, difficulty="low", mastery=None, start=1):
    return UnitScope(identifier, owner, material, 0, (PageSpan(material, start, start + pages - 1),), difficulty, mastery)


def test_exact_decimal_boundaries_and_display_rounding():
    assert exact(Decimal("100.00000000000000001")) > 100
    assert exact(Decimal("-1e-1000")) < 0
    assert exact(0.1) == Fraction(1, 10)
    assert half_up(Fraction(29, 2)) == 15 and half_up(Fraction(119, 2)) == 60
    assert half_up(Decimal("14.49999999999999999")) == 14


@pytest.mark.parametrize("value", [True, "2.5", None, float("nan"), float("inf"), Decimal("NaN"), Decimal("sNaN")])
def test_numbers_reject_coercion_and_nonfinite_values(value):
    with pytest.raises(ValueError):
        exact(value)


@pytest.mark.parametrize("target", [-1, 101, Decimal("100.00000000000000001"), Decimal("-1e-1000")])
def test_target_rejects_out_of_range_before_rounding(target):
    with pytest.raises(ValueError):
        subject(target=target)


@pytest.mark.parametrize("speed", [0, -1, False, "2.5"])
def test_baseline_speed_must_be_positive_numeric(speed):
    with pytest.raises(ValueError):
        subject(speed=speed)


def test_inputs_are_immutable_and_do_not_hold_mutable_arrays():
    subjects = [subject()]
    request = AllocationInput(TODAY, subjects, [unit()])
    subjects.clear()
    assert len(request.subjects) == 1 and isinstance(request.units, tuple)
    with pytest.raises(FrozenInstanceError):
        request.start_date = TODAY + timedelta(days=1)
    with pytest.raises(ValueError):
        AllocationInput(datetime(2026, 11, 14), (), ())


def test_slots_clip_and_subtract_overlap_union_without_joining_short_gaps():
    busy = (BusyInterval(TODAY, 0, 490), BusyInterval(TODAY, 500, 540), BusyInterval(TODAY, 530, 600),
            BusyInterval(TODAY, 610, 1440), BusyInterval(TODAY + timedelta(days=1), 480, 1380))
    for ordering in permutations(busy[:3]):
        slots = free_slots(TODAY, (*ordering, *busy[3:]))
        assert [(slot.start, slot.end) for slot in slots] == [(490, 500), (600, 610)]
        assert sum(slot.minutes for slot in slots) == 20


def test_fixed_tasks_consume_only_their_local_date():
    fixed = (FixedTask("past", "a", "study", "completed", TODAY - timedelta(days=1), 480, 540),
             FixedTask("started", "a", "study", "started", TODAY, 500, 560))
    slots = free_slots(TODAY, fixed=fixed)
    assert [(slot.start, slot.end) for slot in slots] == [(480, 500), (560, 1380)]


@pytest.mark.parametrize("start,end", [(-1, 10), (10, 10), (20, 10), (0, 1441), (True, 10)])
def test_busy_interval_rejects_invalid_and_overnight_input(start, end):
    with pytest.raises(ValueError):
        BusyInterval(TODAY, start, end)


def test_take_pages_preserves_material_union_and_holes_without_page_expansion():
    large = 10 ** 100
    source = (PageSpan("b", 1, 2), PageSpan("a", 1, 5), PageSpan("a", 3, 7), PageSpan("a", 10, large))
    taken, rest = take_pages(source, 9)
    assert taken == (PageSpan("a", 1, 7), PageSpan("a", 10, 11))
    assert rest == (PageSpan("a", 12, large), PageSpan("b", 1, 2))
    assert page_count(taken) + page_count(rest) == page_count(source)
    for invalid in (-1, True, 1.5, large + 10):
        with pytest.raises(ValueError):
            take_pages(source, invalid)


def test_mastery_uses_only_measured_units_and_unique_current_scope_pages():
    units = (UnitScope("one", "a", "pdf", 0, (PageSpan("pdf", 1, 10), PageSpan("pdf", 4, 10)), "low", 80),
             unit("two", material="b", pages=30, mastery=40), unit("unknown", material="c", pages=100),
             unit("foreign", owner="b", material="d", pages=100, mastery=100))
    assert mastery_ratio("a", units) == Fraction(1, 2)
    assert mastery_ratio("unmeasured", units) == 0
    assert allocation_score(subject(target=80), TODAY, 20, Fraction(1, 2)) == Fraction(8, 5)
    assert allocation_score(subject(target=0), TODAY, 20, Fraction(1, 2)) == 0
    assert allocation_score(subject(), TODAY + timedelta(days=5), 20, Fraction()) == 0


def test_reverse_exam_reservation_collision_and_same_exam_group():
    subjects = (subject("a", days=2), subject("b", days=3), subject("c", days=3), subject("d", days=5))
    expected = ((TODAY + timedelta(days=4), ("d",)), (TODAY + timedelta(days=1), ("b", "c")))
    for ordering in permutations(subjects):
        assert reservations(ordering, TODAY) == expected


def test_reservation_before_horizon_does_not_underflow_minimum_date():
    subjects = (Subject("past", date.min, 90, True, True),
                Subject("next", date.min + timedelta(days=1), 90, True, True))
    assert reservations(subjects, date.min) == ()


@pytest.mark.parametrize("capacity,focus,other,expected", [
    (100, 100, 100, (80, 20)), (100, 40, 60, (40, 60)), (100, 100, 10, (90, 10)),
    (100, 40, 20, (40, 20)), (100, 0, 200, (0, 100)), (100, 200, 0, (100, 0)), (0, 20, 20, (0, 0)),
])
def test_group_targets_cap_residuals_and_do_not_invent_work(capacity, focus, other, expected):
    assert group_targets(capacity, focus, other) == expected
