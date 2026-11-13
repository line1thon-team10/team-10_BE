"""plan persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class PlansRepository(Repository):
    table = schema.plans


class PlanChangeRequestsRepository(Repository):
    table = schema.plan_change_requests


class PlanProposalsRepository(Repository):
    table = schema.plan_proposals


class DailyPlanTotalsRepository(Repository):
    table = schema.daily_plan_totals
