"""Actual recovery alternatives over one immutable remaining-work snapshot.

A uses the existing free calendar. B excludes whole, explicitly eligible
concept spans in the caller's priority order. C compresses only new study;
separate remedial/review obligations and started placements remain unchanged.
"""
from dataclasses import dataclass, replace
from fractions import Fraction

from app.domain.ranges import PageSpan, covers, intersect, normalize, subtract
from app.domain.scheduling import AllocationInput, AllocationResult, SearchLimit
from app.services.allocator import allocate


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    enabled: bool
    result: AllocationResult | None
    effective_request: AllocationInput
    exclusions: tuple[PageSpan, ...]
    reason: str


def _candidate(request, *, exclusions=(), max_search_nodes):
    result = allocate(request, max_search_nodes=max_search_nodes)
    reason = ' '.join(item.message for item in result.reasons)
    if not reason:
        reason = '모든 남은 범위를 가용 시간에 배치했어요' if result.feasible else '남은 범위를 배치할 수 없어요'
    return RecoveryCandidate(result.feasible, result, request, tuple(exclusions), reason)


def _eligible_exclusions(request, candidates):
    current = normalize(span for unit in request.units for span in unit.ranges)
    protected = normalize(part.span for task in request.fixed if task.kind == 'study' for part in task.ranges)
    seen = ()
    for span in candidates:
        if not isinstance(span, PageSpan):
            raise ValueError('Exclusions must be whole page-backed concept spans')
        # A partially learned concept is not a wholly unlearned candidate. Do
        # not turn it into a smaller exclusion by clipping to remaining pages.
        if not covers([span], current) or intersect([span], protected) or covers([span], seen):
            continue
        yield span
        seen = normalize((*seen, span))


def build_recovery_candidates(request: AllocationInput, *, excludable_ranges=(),
                              max_candidate_attempts=64, max_search_nodes=250_000):
    """Return all three alternatives; resource exhaustion never means disabled.

    The caller must provide only non-emphasized, wholly unlearned concept
    spans, ordered from least important. B tries increasing whole-concept
    prefixes and stops at the first feasible prefix. This prevents arbitrary
    page trimming and makes exclusions directly persistable as concept scope.
    Both attempt and allocator search limits propagate as SearchLimit.
    """
    if not isinstance(request, AllocationInput):
        raise ValueError('AllocationInput is required')
    if type(max_candidate_attempts) is not int or max_candidate_attempts < 1:
        raise ValueError('max_candidate_attempts must be a positive integer')
    if type(max_search_nodes) is not int or max_search_nodes < 1:
        raise ValueError('max_search_nodes must be a positive integer')
    if isinstance(excludable_ranges, (str, bytes)):
        raise ValueError('Exclusions must be whole page-backed concept spans')
    a = _candidate(request, max_search_nodes=max_search_nodes)
    b = RecoveryCandidate(False, None, request, (), '제외할 수 있는 미학습·비강조 개념 범위가 없어요')
    exclusions = []
    for attempt, span in enumerate(_eligible_exclusions(request, excludable_ranges), 1):
        if attempt > max_candidate_attempts:
            raise SearchLimit(max_candidate_attempts, attempt)
        exclusions.append(span)
        reduced = replace(request, units=tuple(replace(unit, ranges=subtract(unit.ranges, exclusions))
                                               for unit in request.units))
        b = _candidate(reduced, exclusions=exclusions, max_search_nodes=max_search_nodes)
        if b.enabled:
            break
    compressed = replace(request, study_time_factor=request.study_time_factor * Fraction(7, 10))
    c = _candidate(compressed, max_search_nodes=max_search_nodes)
    return {'A': a, 'B': b, 'C': c}
