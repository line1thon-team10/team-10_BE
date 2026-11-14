"""Range boundaries and set identities, independently of persistence or task IDs."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction

import pytest

from app.domain.ranges import PageSpan


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
