"""SQLAlchemy Core repository; never commits or opens its own transaction."""
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, Table, and_, select


class Repository:
    table: Table

    def __init__(self, conn: Connection):
        self.conn = conn

    def _key(self, key: Mapping[str, Any]):
        expected = {column.name for column in self.table.primary_key}
        if set(key) != expected or any(value is None for value in key.values()):
            raise ValueError(f"Expected complete primary key: {sorted(expected)}")
        return and_(*(self.table.c[name] == value for name, value in key.items()))

    def get(self, key: Mapping[str, Any], *conditions):
        row = self.conn.execute(select(self.table).where(self._key(key), *conditions)).mappings().first()
        return dict(row) if row is not None else None

    def list(self, *conditions, limit=100, offset=0):
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("limit must be 1..1000 and offset non-negative")
        rows = self.conn.execute(select(self.table).where(*conditions)
                                 .order_by(*self.table.primary_key.columns).limit(limit).offset(offset)).mappings()
        return [dict(row) for row in rows]

    def add(self, values: Mapping[str, Any]):
        self.conn.execute(self.table.insert().values(**values))

    def update(self, key: Mapping[str, Any], values: Mapping[str, Any], *conditions):
        predicate = self._key(key)
        if not values or set(values) & {c.name for c in self.table.primary_key}:
            raise ValueError("Non-empty values without primary-key changes required")
        return self.conn.execute(self.table.update().where(predicate, *conditions).values(**values)).rowcount

    def delete(self, key: Mapping[str, Any], *conditions):
        return self.conn.execute(self.table.delete().where(self._key(key), *conditions)).rowcount
