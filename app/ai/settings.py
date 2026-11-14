"""POL-02 operational budgets, separate from product limits and provider fees."""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import os


def _seconds(value, name: str, maximum: Decimal | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        precise = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not precise.is_finite() or precise <= 0 or (maximum is not None and precise > maximum):
        raise ValueError(f"{name} is outside its allowed time budget")
    seconds = float(precise)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} cannot be represented as a positive finite duration")
    return seconds


@dataclass(frozen=True, slots=True)
class AiSettings:
    request_budget_seconds: float = 20.0
    pdf_job_budget_seconds: float = 300.0
    cleanup_timeout_seconds: float = 1.0

    def __post_init__(self):
        object.__setattr__(self, "request_budget_seconds", _seconds(
            self.request_budget_seconds, "AI_REQUEST_BUDGET_SECONDS", Decimal(25)))
        object.__setattr__(self, "pdf_job_budget_seconds", _seconds(
            self.pdf_job_budget_seconds, "AI_PDF_JOB_BUDGET_SECONDS"))
        object.__setattr__(self, "cleanup_timeout_seconds", _seconds(
            self.cleanup_timeout_seconds, "process cleanup timeout", Decimal(2)))

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None):
        values = os.environ if environ is None else environ
        return cls(request_budget_seconds=values.get("AI_REQUEST_BUDGET_SECONDS", "20"),
                   pdf_job_budget_seconds=values.get("AI_PDF_JOB_BUDGET_SECONDS", "300"))

    def budget_for(self, *, pdf_job: bool = False) -> float:
        if type(pdf_job) is not bool:
            raise ValueError("pdf_job must be boolean")
        return self.pdf_job_budget_seconds if pdf_job else self.request_budget_seconds
