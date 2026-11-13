"""ai persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class AiSavedResultsRepository(Repository):
    table = schema.ai_saved_results


class AiJobsRepository(Repository):
    table = schema.ai_jobs


class AiUsageLogsRepository(Repository):
    table = schema.ai_usage_logs
