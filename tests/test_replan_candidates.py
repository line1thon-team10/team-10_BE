"""Recovery options use actual page packing and preserve separate obligations."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from fractions import Fraction

import pytest

from app.domain.ranges import PageSpan, page_count
from app.domain.scheduling import (
    AllocationInput, BusyInterval, FixedRange, FixedTask, ScopePart, SearchLimit,
    Subject, UnitScope, WorkRequirement,
)
from app.services.replan_candidates import build_recovery_candidates

TODAY = date(2026, 11, 14)


def request(*, pages=30, minutes=60, requirements=(), fixed=()):
    return AllocationInput(TODAY, (Subject('s', TODAY + timedelta(days=1), 90, True, True, 3),),
        (UnitScope('u', 's', 'pdf', 1, (PageSpan('pdf', 1, pages),), 'low'),),
        busy=(BusyInterval(TODAY, 480 + minutes, 1380),), fixed=fixed, requirements=requirements)


def test_b_excludes_whole_concepts_in_priority_order_and_stops_at_first_fit():
    source = request(pages=30, minutes=60)
    first, second, unused = (PageSpan('pdf', 1, 4), PageSpan('pdf', 5, 10), PageSpan('pdf', 11, 15))
    options = build_recovery_candidates(source, excludable_ranges=(first, second, unused))
    assert list(options) == ['A', 'B', 'C']
    assert not options['A'].enabled and not options['C'].enabled
    b = options['B']
    assert b.enabled and b.exclusions == (first, second)
    assert b.effective_request.units[0].ranges == (PageSpan('pdf', 11, 30),)
    assert sum(task.minutes for task in b.result.placements) == 60
    assert sum(task.display_minutes for task in b.result.placements) == 60
    assert source.units[0].ranges == (PageSpan('pdf', 1, 30),)
    with pytest.raises(FrozenInstanceError):
        b.enabled = False


def test_c_compresses_study_but_not_remedial_or_fixed_work():
    work = WorkRequirement('rem', 's', 'remedial', (ScopePart('u', PageSpan('pdf', 1, 5)),))
    fixed = FixedTask('begun', 's', 'study', 'started', TODAY, 480, 495,
                      (FixedRange('u', PageSpan('pdf', 1, 5)),))
    source = request(pages=25, minutes=75, requirements=(work,), fixed=(fixed,))
    options = build_recovery_candidates(source)
    assert not options['A'].enabled and not options['B'].enabled
    c = options['C']
    assert c.enabled
    assert c.result.fixed == (fixed,)
    study = [part for task in c.result.placements if task.kind == 'study' for part in task.parts]
    remedial = [part for task in c.result.placements if task.kind == 'remedial' for part in task.parts]
    assert sum(part.minutes for part in study) == 42
    assert sum(part.minutes for part in remedial) == 15
    assert all(part.baseline_minutes_per_page == 3 for part in (*study, *remedial))
    assert all(part.difficulty_multiplier == Fraction(7, 10) for part in study)
    assert all(part.difficulty_multiplier == 1 for part in remedial)
    assert c.effective_request.requirements == source.requirements
    assert c.effective_request.fixed == source.fixed


def test_all_three_options_can_be_disabled_without_erasing_supplement_obligations():
    work = WorkRequirement('rem', 's', 'remedial', (ScopePart('u', PageSpan('pdf', 1, 5)),))
    source = request(pages=10, minutes=0, requirements=(work,))
    options = build_recovery_candidates(source, excludable_ranges=(PageSpan('pdf', 1, 10),))
    assert all(not candidate.enabled for candidate in options.values())
    assert all(candidate.effective_request.requirements == source.requirements for candidate in options.values())
    assert options['B'].effective_request.units[0].ranges == ()
    assert all(candidate.reason for candidate in options.values())


def test_b_never_clips_partly_learned_or_foreign_pdf_concepts():
    fixed = FixedTask('done', 's', 'study', 'completed', TODAY - timedelta(days=1), 480, 495,
                      (FixedRange('u', PageSpan('pdf', 1, 5)),))
    source = request(pages=15, minutes=30, fixed=(fixed,))
    options = build_recovery_candidates(source, excludable_ranges=(PageSpan('pdf', 1, 10), PageSpan('foreign', 1, 5)))
    assert options['A'].enabled
    assert not options['B'].enabled and options['B'].exclusions == ()
    assert options['B'].effective_request.units == source.units


def test_b_deduplicates_overlapping_concepts_without_losing_whole_span_identity():
    source = request(pages=20, minutes=30)
    one, two, three = PageSpan('pdf', 1, 4), PageSpan('pdf', 3, 8), PageSpan('pdf', 9, 10)
    options = build_recovery_candidates(source, excludable_ranges=(one, one, two, three))
    b = options['B']
    assert b.enabled and b.exclusions == (one, two, three)
    assert page_count(b.exclusions) == 10
    assert sum(task.display_minutes for task in b.result.placements) == 30


def test_search_budget_is_not_reported_as_three_infeasible_options():
    with pytest.raises(SearchLimit):
        build_recovery_candidates(request(), max_search_nodes=1)


def test_whole_concept_attempt_budget_is_not_false_infeasibility():
    with pytest.raises(SearchLimit) as error:
        build_recovery_candidates(request(), excludable_ranges=(PageSpan('pdf', 1, 2), PageSpan('pdf', 3, 10)),
                                  max_candidate_attempts=1)
    assert error.value.limit == 1


@pytest.mark.parametrize('bad', [0, -1, True, 1.5])
def test_invalid_candidate_budget_is_rejected(bad):
    with pytest.raises(ValueError):
        build_recovery_candidates(request(), max_candidate_attempts=bad)


def test_compression_does_not_fake_a_minimum_duration_for_a_short_remainder():
    source = request(pages=5, minutes=15)
    options = build_recovery_candidates(source)
    assert options['A'].enabled
    assert not options['C'].enabled
    assert options['C'].result.placements == ()
    assert sum(page_count(part.span for part in item.parts) for item in options['C'].result.unplaced) == 5


def test_fractional_compression_retains_exact_boundaries_and_display_rounding():
    source = request(pages=9, minutes=20)
    source = replace(source, subjects=(replace(source.subjects[0], minutes_per_page=Fraction(5, 2)),))
    options = build_recovery_candidates(source)
    assert not options['A'].enabled and options['C'].enabled
    result = options['C'].result
    assert len(result.placements) == 1
    placement = result.placements[0]
    assert placement.minutes == Fraction(63, 4)
    assert placement.display_minutes == 16
    assert placement.end <= 500
    assert sum(part.minutes for part in placement.parts) == placement.minutes


def test_compression_preserves_review_and_remedial_as_distinct_obligations():
    source = request(pages=10, minutes=60, requirements=(
        WorkRequirement('rem', 's', 'remedial', (ScopePart('u', PageSpan('pdf', 1, 5)),)),
        WorkRequirement('rev', 's', 'review', (ScopePart('u', PageSpan('pdf', 1, 5)),)),
    ))
    options = build_recovery_candidates(source)
    assert options['C'].enabled
    actual = {kind: sum(task.minutes for task in options['C'].result.placements if task.kind == kind)
              for kind in ('study', 'remedial', 'review')}
    assert actual == {'study': 21, 'remedial': 15, 'review': 15}
    assert {part.requirement_id for task in options['C'].result.placements if task.kind != 'study'
            for part in task.parts} == {'rem', 'rev'}
