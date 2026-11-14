from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from functools import lru_cache
from itertools import permutations
import random

import pytest

from app.domain.ranges import PageSpan, page_count
from app.domain.scheduling import (
    AllocationInput, BusyInterval, FixedRange, FixedTask, ScopePart, SearchLimit, Subject, UnitScope,
    WorkRequirement, allocation_score, exact, free_slots, group_targets, half_up, mastery_ratio, reservations, take_pages,
)
from app.services.allocator import allocate

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


def calendar(windows, days):
    """Block the horizon except the explicitly specified local-minute windows."""
    busy = []
    for offset in range(days):
        day = TODAY + timedelta(days=offset)
        cursor = Fraction(480)
        for start, end in sorted(windows.get(offset, ())):
            if start > cursor:
                busy.append(BusyInterval(day, cursor, start))
            cursor = end
        if cursor < 1380:
            busy.append(BusyInterval(day, cursor, 1380))
    return tuple(busy)


def by_subject(result, day):
    return {owner: sum((task.minutes for task in result.placements if task.day == day and task.subject_id == owner), Fraction())
            for owner in {task.subject_id for task in result.placements}}


def assert_valid(request, result):
    assert result.feasible and result.unplaced == ()
    for placement in result.placements:
        owner = next(subject for subject in request.subjects if subject.id == placement.subject_id)
        assert request.start_date <= placement.day < owner.exam_date
        assert 15 <= placement.minutes <= 60
        assert placement.minutes == sum(part.minutes for part in placement.parts)
        assert any(slot.start <= placement.start < placement.end <= slot.end
                   for slot in free_slots(placement.day, request.busy, request.fixed))
    for i, first in enumerate(result.placements):
        for second in result.placements[i + 1:]:
            assert first.day != second.day or first.end <= second.start or second.end <= first.start
    def pages(spans):
        return {(span.material_id, page) for span in spans for page in range(span.start, span.end + 1)}
    expected = {}
    for owner in request.subjects:
        required = pages(span for unit in request.units if unit.subject_id == owner.id for span in unit.ranges)
        completed = pages(part.span for task in request.fixed if task.subject_id == owner.id and task.kind == "study" for part in task.ranges)
        if required - completed:
            expected[(owner.id, "study", None)] = required - completed
    for work in request.requirements:
        required = pages(part.span for part in work.parts)
        completed = pages(part.span for task in request.fixed if task.subject_id == work.subject_id and task.kind == work.kind
                          for part in task.ranges if part.requirement_id == work.id)
        if required - completed:
            expected[(work.subject_id, work.kind, work.id)] = required - completed
    actual, counts = {}, {}
    for task in result.placements:
        for part in task.parts:
            key = task.subject_id, task.kind, None if task.kind == "study" else part.requirement_id
            actual.setdefault(key, set()).update(pages([part.span]))
            counts[key] = counts.get(key, 0) + part.span.end - part.span.start + 1
    assert actual == expected
    assert all(counts[key] == len(value) for key, value in actual.items())


@pytest.mark.parametrize("pages,minutes", [(6, 15), (24, 60), (25, Fraction(125, 2))])
def test_exact_task_boundaries_and_short_tail_are_repartitioned(pages, minutes):
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=pages),),
                              calendar({0: [(480, 480 + minutes)]}, 1))
    result = allocate(request)
    assert_valid(request, result)
    assert sum(task.minutes for task in result.placements) == minutes
    assert page_count(part.span for task in result.placements for part in task.parts) == pages
    if pages == 25:
        assert len(result.placements) == 2  # 60 + 2.5 is invalid; a different split exists.


def test_short_adjacent_unit_fragments_merge_with_material_identity():
    units = (unit("one", material="a", pages=3), unit("two", material="b", pages=3))
    request = AllocationInput(TODAY, (subject(days=1),), units, calendar({0: [(480, 495)]}, 1))
    result = allocate(request)
    assert_valid(request, result)
    assert len(result.placements) == 1
    assert {part.span for part in result.placements[0].parts} == {PageSpan("a", 1, 3), PageSpan("b", 1, 3)}


def test_overlapping_concept_ranges_count_once_but_other_material_pages_remain():
    units = (UnitScope("one", "a", "a", 0, (PageSpan("a", 1, 4), PageSpan("a", 3, 8)), "low"),
             unit("two", material="b", pages=8))
    request = AllocationInput(TODAY, (subject(days=1),), units, calendar({0: [(480, 520)]}, 1))
    result = allocate(request)
    assert_valid(request, result)
    assert sum(task.minutes for task in result.placements) == 40
    assert page_count(part.span for task in result.placements for part in task.parts) == 16


@pytest.mark.parametrize("speed", [Decimal("14.99999999999999999"), Decimal("60.00000000000000001"), 100])
def test_display_rounding_cannot_make_an_invalid_single_page_task_feasible(speed):
    request = AllocationInput(TODAY, (subject(days=1, speed=speed),), (unit(pages=1),))
    result = allocate(request)
    assert not result.feasible and not result.placements
    assert result.unplaced[0].parts == (ScopePart("u", PageSpan("pdf", 1, 1)),)


def test_two_ten_minute_gaps_do_not_become_one_twenty_minute_slot():
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=6),),
                              calendar({0: [(480, 490), (500, 510)]}, 1))
    result = allocate(request)
    assert not result.feasible and result.reasons[0].code == "PLAN_INFEASIBLE"
    assert page_count(part.span for item in result.unplaced for part in item.parts) == 6


def test_complete_search_recovers_from_a_greedy_slot_choice():
    # Putting A's 15-minute page in the first 20-minute slot strands B.
    request = AllocationInput(TODAY, (subject("a", days=1, speed=15), subject("b", days=1, speed=20)),
                              (unit("ua", "a", material="a", pages=1), unit("ub", "b", material="b", pages=1)),
                              calendar({0: [(480, 500), (510, 525)]}, 1))
    result = allocate(request)
    assert_valid(request, result)
    assert [(task.subject_id, task.start, task.minutes) for task in result.placements] == [("b", 480, 20), ("a", 510, 15)]


def test_daily_relative_score_and_zero_score_subjects_are_preserved():
    subjects = (subject("a", days=2, target=90), subject("b", days=2, target=30))
    units = (unit("ua", "a", material="a"), unit("ub", "b", material="b"))
    request = AllocationInput(TODAY, subjects, units, calendar({0: [(480, 570)], 1: [(480, 540)]}, 2))
    result = allocate(request)
    assert_valid(request, result)
    assert by_subject(result, TODAY + timedelta(days=1)) == {"a": 45, "b": 15}
    zero_request = AllocationInput(TODAY, (subject("a", days=1, target=0), subject("b", days=1)),
                                  (unit("ua", "a", material="a", pages=6), unit("ub", "b", material="b", pages=6, mastery=100)))
    zero = allocate(zero_request)
    assert_valid(zero_request, zero)
    assert by_subject(zero, TODAY) == {"a": 15, "b": 15}


def test_reservation_ratio_uses_unrounded_minutes_not_pages():
    request = AllocationInput(TODAY, (subject("a", days=2, speed=5), subject("b", days=5)),
                              (unit("ua", "a", material="a", pages=20), unit("ub", "b", material="b", pages=20)),
                              calendar({0: [(480, 530)], 1: [(480, 580)]}, 5))
    result = allocate(request)
    assert_valid(request, result)
    assert by_subject(result, TODAY + timedelta(days=1)) == {"a": 80, "b": 20}


def test_reservation_quantization_reports_exact_minimum_difference():
    request = AllocationInput(TODAY, (subject("a", days=2), subject("b", days=5)),
                              (unit("ua", "a", material="a", pages=30, difficulty="high"), unit("ub", "b", material="b", pages=20)),
                              calendar({0: [(480, 544)], 1: [(480, 580)]}, 5))
    result = allocate(request)
    assert_valid(request, result)
    assert by_subject(result, TODAY + timedelta(days=1)) == {"a": Fraction(315, 4), "b": 20}
    assert [(reason.day, reason.deviation_minutes) for reason in result.reasons] == [(TODAY + timedelta(days=1), Fraction(5, 4))]


def test_colliding_reservations_give_later_exam_the_first_eighty_percent():
    request = AllocationInput(TODAY, (subject("a", days=2), subject("b", days=3)),
                              (unit("ua", "a", material="a", pages=8), unit("ub", "b", material="b", pages=32)),
                              calendar({1: [(480, 580)]}, 3))
    result = allocate(request)
    assert_valid(request, result)
    assert by_subject(result, TODAY + timedelta(days=1)) == {"a": 20, "b": 80}


def test_complete_required_scope_takes_precedence_over_reservation_preference():
    request = AllocationInput(TODAY, (subject("a", days=2), subject("b", days=3)),
                              (unit("ua", "a", material="a", pages=32), unit("ub", "b", material="b", pages=32)),
                              calendar({1: [(480, 580)], 2: [(480, 540)]}, 3))
    result = allocate(request)
    assert_valid(request, result)
    assert by_subject(result, TODAY + timedelta(days=1)) == {"a": 80, "b": 20}
    assert result.reasons[0].deviation_minutes == 120


def test_search_limit_is_distinct_from_a_proven_infeasible_result():
    request = AllocationInput(TODAY, (subject(),), (unit(),))
    with pytest.raises(SearchLimit) as raised:
        allocate(request, max_search_nodes=1)
    assert raised.value.limit == 1 and raised.value.explored > 1


@pytest.mark.parametrize("difficulty,pages", [(None, 5), ("mid", 5), ("high", 4)])
def test_difficulty_uses_baseline_once_and_snapshots_exact_rate(difficulty, pages):
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=pages, difficulty=difficulty),),
                              calendar({0: [(480, 495)]}, 1))
    result = allocate(request)
    assert_valid(request, result)
    assert result.placements[0].minutes == 15
    assert result.placements[0].parts[0].baseline_minutes_per_page == Fraction(5, 2)
    assert result.placements[0].parts[0].difficulty_multiplier == (Fraction(3, 2) if difficulty == "high" else Fraction(6, 5))


def test_fixed_eighty_minutes_count_toward_reservation_and_remain_unchanged():
    day = TODAY + timedelta(days=1)
    fixed = (FixedTask("first", "a", "study", "completed", day, 480, 520, (FixedRange("ua", PageSpan("a", 1, 16)),)),
             FixedTask("second", "a", "study", "started", day, 520, 560, (FixedRange("ua", PageSpan("a", 17, 32)),)))
    request = AllocationInput(TODAY, (subject("a", days=2), subject("b", days=5)),
                              (unit("ua", "a", material="a", pages=32), unit("ub", "b", material="b", pages=8)),
                              calendar({1: [(480, 580)]}, 5), fixed)
    result = allocate(request)
    assert_valid(request, result)
    assert result.fixed == fixed and result.fixed[0] is fixed[0]
    assert [(task.subject_id, task.start, task.minutes) for task in result.placements] == [("b", 560, 20)]
    assert not result.reasons


def test_fixed_reservation_deviation_is_explained_without_moving_fixed_work():
    day = TODAY + timedelta(days=1)
    fixed = (FixedTask("first", "a", "study", "completed", day, 480, 525, (FixedRange("ua", PageSpan("a", 1, 18)),)),
             FixedTask("second", "a", "study", "started", day, 525, 570, (FixedRange("ua", PageSpan("a", 19, 36)),)))
    request = AllocationInput(TODAY, (subject("a", days=2), subject("b", days=5)),
                              (unit("ua", "a", material="a", pages=36), unit("ub", "b", material="b", pages=6)),
                              calendar({0: [(480, 495)], 1: [(480, 580)]}, 5), fixed)
    result = allocate(request)
    assert_valid(request, result)
    assert result.fixed == fixed and result.placements[0].day == TODAY
    assert result.reasons[0].deviation_minutes == 20
    assert "고정" in result.reasons[0].message


def test_past_completed_study_does_not_remove_separate_remedial_obligation():
    fixed = (FixedTask("past", "a", "study", "completed", TODAY - timedelta(days=1), 480, 495,
                       (FixedRange("u", PageSpan("pdf", 1, 6)),)),)
    work = WorkRequirement("remedial", "a", "remedial", (ScopePart("u", PageSpan("pdf", 1, 6)),))
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=6),),
                              calendar({0: [(480, 495)]}, 1), fixed, (work,))
    result = allocate(request)
    assert_valid(request, result)
    assert len(result.placements) == 1 and result.placements[0].kind == "remedial"
    assert result.placements[0].parts[0].requirement_id == "remedial"


def test_only_matching_remedial_obligation_is_satisfied_by_fixed_remedial_work():
    fixed = (FixedTask("past-study", "a", "study", "completed", TODAY - timedelta(days=2), 480, 495,
                       (FixedRange("u", PageSpan("pdf", 1, 6)),)),
             FixedTask("past-remedial", "a", "remedial", "completed", TODAY - timedelta(days=1), 480, 495,
                       (FixedRange("u", PageSpan("pdf", 1, 6), "one"),)))
    works = tuple(WorkRequirement(key, "a", "remedial", (ScopePart("u", PageSpan("pdf", 1, 6)),)) for key in ("one", "two"))
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=6),),
                              calendar({0: [(480, 495)]}, 1), fixed, works)
    result = allocate(request)
    assert_valid(request, result)
    assert {part.requirement_id for task in result.placements for part in task.parts} == {"two"}


def test_infeasible_remedial_obligation_is_returned_in_full():
    work = WorkRequirement("must-keep", "a", "remedial", (ScopePart("u", PageSpan("pdf", 1, 6)),))
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=6),),
                              calendar({0: [(480, 495)]}, 1), requirements=(work,))
    result = allocate(request)
    assert not result.feasible and result.placements == ()
    assert {item.requirement_id for item in result.unplaced} == {"study:u", "must-keep"}


def test_release_date_and_exam_day_are_hard_limits():
    work = WorkRequirement("later", "a", "remedial", (ScopePart("u", PageSpan("pdf", 1, 6)),), TODAY + timedelta(days=1))
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=6),), requirements=(work,))
    result = allocate(request)
    assert not result.feasible and any(item.requirement_id == "later" for item in result.unplaced)
    expired = AllocationInput(TODAY, (subject(days=0),), (unit(pages=6),))
    assert not allocate(expired).feasible


@pytest.mark.parametrize("bad", [Subject("a", TODAY + timedelta(days=1), 90),
                                  Subject("a", None, 90, True, True), Subject("a", TODAY + timedelta(days=1), None, True, True)])
def test_unconfirmed_or_missing_goal_is_not_approved(bad):
    result = allocate(AllocationInput(TODAY, (bad,), (unit(pages=6),)))
    assert not result.feasible and result.reasons[0].code == "UNCONFIRMED_INPUT"


def test_conflicting_fixed_work_is_preserved_in_an_infeasible_result():
    fixed = (FixedTask("started", "a", "study", "started", TODAY, 480, 495),)
    request = AllocationInput(TODAY, (subject(days=1),), (unit(pages=6),), (BusyInterval(TODAY, 490, 500),), fixed)
    result = allocate(request)
    assert not result.feasible and result.fixed == fixed
    assert result.reasons[0].code == "FIXED_CONFLICT"


def test_input_permutation_preserves_ids_ranges_order_and_reasons():
    subjects = (subject("b", days=2), subject("a", days=2), subject("c", days=2))
    units = tuple(unit(f"u{s.id}", s.id, material=s.id, pages=6) for s in subjects)
    busy = calendar({0: [(480, 510)], 1: [(480, 510)]}, 2)
    expected = allocate(AllocationInput(TODAY, subjects, units, busy))
    for ordering in permutations(subjects):
        result = allocate(AllocationInput(TODAY, ordering, tuple(reversed(units)), tuple(reversed(busy))))
        assert result == expected


def brute_pack(pages, slots):
    """Independent tiny oracle: enumerate subsets of individual pages as tasks."""
    @lru_cache(None)
    def search(mask, slot_index, used):
        if mask == 0:
            return True
        if slot_index == len(slots):
            return False
        day, capacity = slots[slot_index]
        subset = mask
        while subset:
            chosen = [pages[i] for i in range(len(pages)) if subset & (1 << i)]
            owners = {page[0] for page in chosen}
            duration = sum(page[1] for page in chosen)
            if len(owners) == 1 and 15 <= duration <= 60 and used + duration <= capacity and all(
                page[3] <= day < page[2] for page in chosen
            ) and search(mask ^ subset, slot_index, used + duration):
                return True
            subset = (subset - 1) & mask
        return search(mask, slot_index + 1, Fraction())
    return search((1 << len(pages)) - 1, 0, Fraction())


def test_small_counterexamples_match_independent_complete_page_oracle():
    rng = random.Random(20261114)
    for _ in range(100):
        counts = [rng.randint(1, 2), rng.randint(1, 2)]
        costs = [rng.choice((5, Fraction(15, 2), 10, 15, 20, 25)) for _ in range(2)]
        deadlines = [rng.randint(1, 2), rng.randint(1, 2)]
        windows, slots = {}, []
        for day in range(2):
            capacity = rng.choice((0, 10, 15, 20, 25, 30, 40))
            windows[day] = [(480, 480 + capacity)] if capacity else []
            if capacity:
                slots.append((day, capacity))
        pages = tuple((owner, costs[owner], deadlines[owner], 0) for owner in range(2) for _ in range(counts[owner]))
        expected = brute_pack(pages, tuple(slots))
        request = AllocationInput(TODAY,
            tuple(subject(str(i), days=deadlines[i], speed=costs[i]) for i in range(2)),
            tuple(unit(f"u{i}", str(i), material=str(i), pages=counts[i]) for i in range(2)), calendar(windows, 2))
        actual = allocate(request)
        assert actual.feasible == expected, (counts, costs, deadlines, windows)
        if expected:
            assert_valid(request, actual)


def test_reservation_objective_matches_independent_enumerated_day_assignments():
    pages = tuple([("a", Fraction(15, 2), 2, 0)] * 4 + [("b", Fraction(5), 4, 0)] * 6)
    target_a, target_b = Fraction(28), Fraction(7)
    best = None
    for mask in range(1 << len(pages)):
        late = tuple(page for i, page in enumerate(pages) if mask & (1 << i))
        early = tuple(page for i, page in enumerate(pages) if not mask & (1 << i))
        if brute_pack(late, ((1, 35),)) and brute_pack(early, ((0, 35),)):
            a = sum((page[1] for page in late if page[0] == "a"), Fraction())
            b = sum((page[1] for page in late if page[0] == "b"), Fraction())
            error = abs(a - target_a) + abs(b - target_b)
            best = error if best is None else min(best, error)
    request = AllocationInput(TODAY, (subject("a", days=2, speed=Fraction(15, 2)), subject("b", days=4, speed=5)),
                              (unit("ua", "a", material="a", pages=4), unit("ub", "b", material="b", pages=6)),
                              calendar({0: [(480, 515)], 1: [(480, 515)]}, 4))
    result = allocate(request)
    assert_valid(request, result)
    assert result.reasons[0].deviation_minutes == best == 9


def test_realistic_three_subject_thirty_page_input():
    subjects = (subject("a", days=4), subject("b", days=6), subject("c", days=8))
    units = (unit("ua", "a", material="a", difficulty="low"), unit("ub", "b", material="b", difficulty="mid"),
             unit("uc", "c", material="c", difficulty="high"))
    request = AllocationInput(TODAY, subjects, units,
                              calendar({day: [(1080, 1260)] for day in range(8)}, 8))
    result = allocate(request)
    assert_valid(request, result)
    assert sum(task.minutes for task in result.placements) == Fraction(555, 2)
