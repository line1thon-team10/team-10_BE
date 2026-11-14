"""Range boundaries and set identities, independently of persistence or task IDs."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
from itertools import combinations, permutations

import pytest

from app.domain.ranges import PageSpan, normalize, page_count, subtract


@pytest.mark.parametrize("material_id", ["pdf-a", "PDF-a", "강의자료", " pdf-a "])
def test_page_span_preserves_material_identity(material_id):
    span = PageSpan(material_id, 1, 1)

    assert (span.material_id, span.start, span.end) == (material_id, 1, 1)


def test_page_span_is_an_immutable_hashable_value():
    span = PageSpan("pdf-a", 2, 9)

    assert span == PageSpan("pdf-a", 2, 9)
    assert len({span, PageSpan("pdf-a", 2, 9), PageSpan("pdf-b", 2, 9)}) == 2
    for attribute, value in [("material_id", "pdf-b"), ("start", 3), ("end", 8)]:
        with pytest.raises(FrozenInstanceError):
            setattr(span, attribute, value)
        with pytest.raises(FrozenInstanceError):
            delattr(span, attribute)


@pytest.mark.parametrize("material_id", ["", " \t\n", None, False, 1, b"pdf-a", [], {}])
def test_page_span_rejects_missing_or_nonstring_material_identity(material_id):
    with pytest.raises(ValueError):
        PageSpan(material_id, 1, 2)


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize(
    "value",
    [True, False, 1.0, 1.5, "1", None, float("nan"), float("inf"), Decimal(1), Fraction(1)],
)
def test_page_span_rejects_coercible_or_noninteger_pages(field, value):
    bounds = {"start": 1, "end": 2, field: value}

    with pytest.raises(ValueError):
        PageSpan("pdf-a", **bounds)


@pytest.mark.parametrize("start,end", [(0, 1), (-1, 1), (1, 0), (1, -1), (2, 1), (0, 0)])
def test_page_span_rejects_zero_negative_or_reversed_bounds(start, end):
    with pytest.raises(ValueError):
        PageSpan("pdf-a", start, end)


def test_page_span_accepts_arbitrary_precision_integer_bounds():
    page = 10**100

    assert PageSpan("pdf-a", page, page + 1).end == page + 1


def _pages(spans):
    """Small fixtures only: a deliberately page-based oracle for interval algebra."""
    return {(span.material_id, page) for span in spans for page in range(span.start, span.end + 1)}


def _assert_canonical(spans):
    assert isinstance(spans, tuple)
    assert spans == tuple(sorted(spans, key=lambda span: (span.material_id, span.start, span.end)))
    for previous, current in zip(spans, spans[1:]):
        assert previous.material_id != current.material_id or previous.end + 1 < current.start


@pytest.mark.parametrize(
    "bounds,expected",
    [
        ([], []),
        ([(3, 3)], [(3, 3)]),
        ([(1, 3), (3, 5)], [(1, 5)]),
        ([(1, 3), (4, 5)], [(1, 5)]),
        ([(1, 3), (5, 5)], [(1, 3), (5, 5)]),
        ([(1, 9), (3, 4), (1, 9)], [(1, 9)]),
        ([(7, 9), (1, 3), (4, 6)], [(1, 9)]),
    ],
)
def test_normalize_merges_overlap_and_adjacency_but_preserves_a_one_page_gap(bounds, expected):
    spans = [PageSpan("pdf-a", start, end) for start, end in bounds]

    assert normalize(spans) == tuple(PageSpan("pdf-a", start, end) for start, end in expected)


def test_normalize_preserves_pdf_identity_and_sorts_by_identity_then_page():
    spans = [PageSpan("pdf-b", 1, 5), PageSpan("pdf-a", 8, 9), PageSpan("pdf-a", 1, 5)]
    before = spans.copy()

    assert normalize(spans) == (PageSpan("pdf-a", 1, 5), PageSpan("pdf-a", 8, 9), PageSpan("pdf-b", 1, 5))
    assert spans == before


@pytest.mark.parametrize("factory", [list, tuple, set, iter, lambda spans: (span for span in spans)])
def test_normalize_accepts_iterables_without_requiring_a_sequence(factory):
    spans = [PageSpan("pdf-a", 2, 3), PageSpan("pdf-a", 1, 1)]

    assert normalize(factory(spans)) == (PageSpan("pdf-a", 1, 3),)


class _SingleUseIterable:
    def __init__(self, spans):
        self.spans = spans
        self.iterated = False

    def __iter__(self):
        assert not self.iterated, "the input iterable was consumed more than once"
        self.iterated = True
        return iter(self.spans)


def test_normalize_consumes_the_input_once():
    spans = _SingleUseIterable([PageSpan("pdf-a", 4, 5), PageSpan("pdf-a", 1, 3)])

    assert normalize(spans) == (PageSpan("pdf-a", 1, 5),)


@pytest.mark.parametrize("spans", [None, 1, PageSpan("pdf-a", 1, 1), "", "pdf-a", b"", bytearray()])
def test_normalize_rejects_non_span_containers(spans):
    with pytest.raises(ValueError):
        normalize(spans)


@pytest.mark.parametrize("invalid", [None, True, 1, "pdf-a", ("pdf-a", 1, 2), {"material_id": "pdf-a", "start": 1, "end": 2}])
def test_normalize_rejects_invalid_items_even_after_valid_spans(invalid):
    with pytest.raises(ValueError):
        normalize(iter([PageSpan("pdf-a", 1, 2), invalid]))


def test_normalize_is_permutation_invariant_and_idempotent():
    spans = [PageSpan("pdf-a", 1, 3), PageSpan("pdf-a", 3, 5), PageSpan("pdf-b", 2, 2)]
    expected = (PageSpan("pdf-a", 1, 5), PageSpan("pdf-b", 2, 2))

    for ordering in permutations(spans):
        assert normalize(ordering) == expected
    assert normalize(expected) == expected


def test_normalize_preserves_page_sets_for_all_subsets_of_small_mixed_pdf_intervals():
    candidates = [PageSpan(material, start, end) for material in ["pdf-a", "pdf-b"] for start, end in [(1, 2), (2, 3), (5, 5)]]

    for size in range(len(candidates) + 1):
        for spans in combinations(candidates, size):
            result = normalize(reversed(spans))
            _assert_canonical(result)
            assert _pages(result) == _pages(spans)


def test_normalize_handles_huge_intervals_without_expanding_pages():
    end = 10**100

    assert normalize([PageSpan("pdf-a", end, end), PageSpan("pdf-a", 1, end - 1)]) == (PageSpan("pdf-a", 1, end),)


@pytest.mark.parametrize(
    "spans,expected",
    [
        ([], 0),
        ([PageSpan("pdf-a", 7, 7)], 1),
        ([PageSpan("pdf-a", 1, 3), PageSpan("pdf-a", 2, 4), PageSpan("pdf-b", 1, 2)], 6),
        ([PageSpan("pdf-a", 1, 3), PageSpan("pdf-a", 5, 6)], 5),
        ([PageSpan("pdf-a", 1, 3)] * 3, 3),
    ],
)
def test_page_count_counts_unique_pages_with_inclusive_bounds(spans, expected):
    result = page_count(_SingleUseIterable(spans))

    assert type(result) is int
    assert result == expected
    assert result == len(_pages(spans))


def test_page_count_is_additive_across_materials():
    first_pdf = [PageSpan("pdf-a", 2, 5), PageSpan("pdf-a", 4, 7)]
    second_pdf = [PageSpan("pdf-b", 2, 5), PageSpan("pdf-b", 7, 7)]

    assert page_count(first_pdf + second_pdf) == page_count(first_pdf) + page_count(second_pdf) == 11


def test_page_count_handles_huge_intervals_without_expanding_pages():
    end = 10**100

    assert page_count([PageSpan("pdf-a", 1, end), PageSpan("pdf-a", 2, end), PageSpan("pdf-b", end, end)]) == end + 1


@pytest.mark.parametrize("spans", [None, "", [None], [PageSpan("pdf-a", 1, 1), ("pdf-a", 1, 2)]])
def test_page_count_rejects_invalid_inputs(spans):
    with pytest.raises(ValueError):
        page_count(spans)


@pytest.mark.parametrize(
    "sources,removals,expected",
    [
        ([], [], []),
        ([], [(1, 3)], []),
        ([(1, 3)], [], [(1, 3)]),
        ([(1, 3)], [(4, 5)], [(1, 3)]),
        ([(4, 5)], [(1, 3)], [(4, 5)]),
        ([(1, 3)], [(1, 3)], []),
        ([(3, 5)], [(1, 9)], []),
        ([(1, 5)], [(1, 1)], [(2, 5)]),
        ([(1, 5)], [(5, 5)], [(1, 4)]),
        ([(3, 7)], [(1, 4)], [(5, 7)]),
        ([(3, 7)], [(6, 9)], [(3, 5)]),
        ([(1, 5)], [(3, 3)], [(1, 2), (4, 5)]),
        ([(1, 10)], [(2, 3), (7, 8)], [(1, 1), (4, 6), (9, 10)]),
        ([(1, 3), (5, 7), (9, 11)], [(2, 10)], [(1, 1), (11, 11)]),
        ([(1, 5), (3, 7)], [(2, 3), (3, 5)], [(1, 1), (6, 7)]),
    ],
)
def test_subtract_handles_inclusive_boundaries_and_splits(sources, removals, expected):
    source_spans = [PageSpan("pdf-a", start, end) for start, end in sources]
    removed_spans = [PageSpan("pdf-a", start, end) for start, end in removals]

    result = subtract(_SingleUseIterable(source_spans), _SingleUseIterable(removed_spans))

    assert result == tuple(PageSpan("pdf-a", start, end) for start, end in expected)
    _assert_canonical(result)


def test_subtract_never_removes_matching_pages_of_another_pdf():
    sources = [PageSpan("pdf-c", 1, 3), PageSpan("pdf-a", 1, 3)]
    removals = [PageSpan("pdf-b", 1, 3), PageSpan("pdf-a", 2, 2), PageSpan("pdf-d", 1, 3)]

    assert subtract(sources, removals) == (PageSpan("pdf-a", 1, 1), PageSpan("pdf-a", 3, 3), PageSpan("pdf-c", 1, 3))


def test_subtract_is_empty_for_a_shared_single_use_iterable():
    spans = _SingleUseIterable([PageSpan("pdf-a", 1, 3)])

    assert subtract(spans, spans) == ()


def _small_collections():
    candidates = [PageSpan(material, start, end) for material in ["pdf-a", "pdf-b"] for start, end in [(1, 2), (2, 3), (5, 5)]]
    return [spans for size in range(3) for spans in combinations(candidates, size)]


def test_subtract_matches_set_difference_for_small_interval_combinations():
    collections = _small_collections()
    for sources in collections:
        for removals in collections:
            remaining = subtract(sources, removals)
            _assert_canonical(remaining)
            assert _pages(remaining) == _pages(sources) - _pages(removals)
            assert subtract(remaining, removals) == remaining


def test_subtract_handles_huge_intervals_without_expanding_pages():
    end = 10**100
    middle = end // 2

    result = subtract(
        [PageSpan("pdf-a", 1, end)],
        [PageSpan("pdf-a", 1, 1), PageSpan("pdf-a", middle, middle), PageSpan("pdf-a", end, end)],
    )

    assert result == (PageSpan("pdf-a", 2, middle - 1), PageSpan("pdf-a", middle + 1, end - 1))
    assert page_count(result) == end - 3


@pytest.mark.parametrize("invalid", [None, "", [None], [PageSpan("pdf-a", 1, 1), ("pdf-a", 1, 2)]])
@pytest.mark.parametrize("side", ["source", "removed"])
def test_subtract_validates_both_inputs_even_if_the_other_is_empty(invalid, side):
    with pytest.raises(ValueError):
        subtract(invalid, []) if side == "source" else subtract([], invalid)
