"""subject persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class SubjectsRepository(Repository):
    table = schema.subjects
