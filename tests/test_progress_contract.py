"""Separate placement achievement, unique study progress and quiz eligibility."""
from fractions import Fraction

import pytest

from app.domain.progress import PageProgress, rounded_percent
from app.domain.ranges import PageSpan


@pytest.mark.parametrize("done,total,rate,opened,remaining", [
    (0, 0, 0, False, 0), (10, 0, 0, False, 0),
    (1, 8, 13, False, 5), (6999, 10000, 70, False, 1),
    (7, 10, 70, True, 0), (101, 100, 100, True, 0),
    (7 * 10 ** 30 - 1, 10 ** 31, 70, False, 1),
])
def test_page_ratio_keeps_display_and_raw_threshold_separate(done, total, rate, opened, remaining):
    progress = PageProgress(done, total)
    assert progress.rate == rate
    assert progress.reaches(70) is opened
    assert progress.remaining_pages(70) == remaining


@pytest.mark.parametrize("invalid", [-1, True, 1.0, "1", None, float("nan"), float("inf")])
def test_page_counts_are_nonnegative_integers(invalid):
    with pytest.raises(ValueError):
        PageProgress(invalid, 10)
    with pytest.raises(ValueError):
        PageProgress(1, invalid)


@pytest.mark.parametrize("invalid", [-1, 101, True, 70.0, "70", None])
def test_thresholds_reject_ambiguous_inputs(invalid):
    progress = PageProgress(7, 10)
    with pytest.raises(ValueError):
        progress.reaches(invalid)
    with pytest.raises(ValueError):
        progress.remaining_pages(invalid)


def test_unique_progress_intersects_scope_and_preserves_pdf_identity():
    progress = PageProgress.from_ranges(
        iter([PageSpan("a", 1, 5), PageSpan("a", 4, 10), PageSpan("b", 1, 10)]),
        [PageSpan("a", 4, 30), PageSpan("a", 5, 8), PageSpan("b", 1, 5), PageSpan("c", 1, 10)],
    )
    assert (progress.done_pages, progress.total_pages, progress.rate) == (12, 20, 60)


def test_percentage_display_uses_half_up_and_bounds():
    assert rounded_percent(Fraction(1, 16), places=1) == 6.3
    assert rounded_percent(Fraction(1, 8)) == 13
    assert rounded_percent(Fraction(2), places=1) == 100.0
    assert rounded_percent(Fraction(-1)) == 0
