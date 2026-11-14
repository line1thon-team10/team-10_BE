"""Exact page ratios: display rounding never decides whether an action is open."""
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from app.domain.ranges import PageSpan, intersect, page_count


def rounded_percent(ratio: Fraction, *, places: int = 0) -> int | float:
    """Display a bounded percentage with HALF_UP, without float intermediates."""
    if type(places) is not int or places < 0:
        raise ValueError("places must be a nonnegative integer")
    scale = 10 ** places
    value = min(Fraction(1), max(Fraction(0), ratio)) * 100 * scale
    rounded = (2 * value.numerator + value.denominator) // (2 * value.denominator)
    return rounded / scale if places else rounded


@dataclass(frozen=True, slots=True)
class PageProgress:
    done_pages: int
    total_pages: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (self.done_pages, self.total_pages)):
            raise ValueError("page counts must be nonnegative integers")

    @classmethod
    def from_ranges(cls, planned: Iterable[PageSpan], completed: Iterable[PageSpan]) -> "PageProgress":
        scope = tuple(planned)
        return cls(page_count(intersect(scope, completed)), page_count(scope))

    @property
    def ratio(self) -> Fraction:
        return Fraction(min(self.done_pages, self.total_pages), self.total_pages) if self.total_pages else Fraction(0)

    @property
    def rate(self) -> int:
        return rounded_percent(self.ratio)

    def reaches(self, percent: int) -> bool:
        self._check_threshold(percent)
        return self.total_pages > 0 and self.done_pages * 100 >= self.total_pages * percent

    def remaining_pages(self, percent: int) -> int:
        self._check_threshold(percent)
        return max(0, (self.total_pages * percent + 99) // 100 - self.done_pages)

    @staticmethod
    def _check_threshold(percent: int) -> None:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("threshold must be an integer in 0..100")
