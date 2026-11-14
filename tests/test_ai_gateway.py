from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.ai.settings import AiSettings


def test_operational_budgets_have_separate_sync_and_pdf_defaults():
    settings = AiSettings.from_environment({})
    assert settings.budget_for() == 20
    assert settings.budget_for(pdf_job=True) == 300
    with pytest.raises(FrozenInstanceError):
        settings.request_budget_seconds = 30


def test_budget_environment_parsing_and_allowed_sync_upper_bound():
    settings = AiSettings.from_environment({"AI_REQUEST_BUDGET_SECONDS": "25", "AI_PDF_JOB_BUDGET_SECONDS": "180.5"})
    assert settings.request_budget_seconds == 25 and settings.pdf_job_budget_seconds == 180.5


@pytest.mark.parametrize("value", [0, -1, True, "", "   ", "NaN", "Infinity", "bad", None,
                                  Decimal("1e-1000"), Decimal("1e1000")])
def test_time_budgets_reject_unusable_or_nonfinite_values(value):
    for field in ("request_budget_seconds", "pdf_job_budget_seconds", "cleanup_timeout_seconds"):
        with pytest.raises(ValueError):
            AiSettings(**{field: value})


@pytest.mark.parametrize("value", [26, Decimal("25.000000000000000001")])
def test_sync_budget_checks_limit_before_float_conversion(value):
    with pytest.raises(ValueError):
        AiSettings(request_budget_seconds=value)


def test_pdf_budget_selection_does_not_accept_truthy_nonboolean_input():
    with pytest.raises(ValueError):
        AiSettings().budget_for(pdf_job="true")
