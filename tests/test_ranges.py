"""Range boundaries and set identities, independently of persistence or task IDs."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
from itertools import combinations, permutations

import pytest

from app.domain.ranges import PageSpan, normalize


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
