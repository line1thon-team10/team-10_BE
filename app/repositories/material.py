"""material persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class MaterialsRepository(Repository):
    table = schema.materials


class UnitsRepository(Repository):
    table = schema.units


class ConceptsRepository(Repository):
    table = schema.concepts
