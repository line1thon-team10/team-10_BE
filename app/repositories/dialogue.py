"""dialogue persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class TutorMessagesRepository(Repository):
    table = schema.tutor_messages


class TutorSourcesRepository(Repository):
    table = schema.tutor_sources


class ReflectionReportsRepository(Repository):
    table = schema.reflection_reports
