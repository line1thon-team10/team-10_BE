"""Owned onboarding reads/writes; the caller controls the transaction and lock."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Connection, func, or_, select, update

from app.db import schema as t


class OnboardingRepository:
    def __init__(self, connection: Connection, user_id: str):
        self.connection = connection
        self.user_id = user_id

    def subject(self, subject_id: str) -> dict | None:
        row = self.connection.execute(select(t.subjects).where(
            t.subjects.c.user_id == self.user_id, t.subjects.c.id == subject_id)).mappings().first()
        return dict(row) if row else None

    def subjects(self, status: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute(select(t.subjects).where(
            t.subjects.c.user_id == self.user_id, t.subjects.c.status == status)
            .order_by(t.subjects.c.exam_date.asc().nulls_last(), t.subjects.c.id)).mappings()]

    def _context(self, subject_id: str | None):
        columns = t.onboarding_messages.c
        context = columns.subject_id == subject_id if subject_id else columns.subject_id.is_(None)
        # Old proposal-linked conversations are preserved but cannot be guessed
        # into the initial onboarding context.
        return (columns.user_id == self.user_id, context, columns.change_request_id.is_(None))

    def messages(self, subject_id: str | None) -> list[dict]:
        columns = t.onboarding_messages.c
        return [dict(row) for row in self.connection.execute(select(t.onboarding_messages)
            .where(*self._context(subject_id))
            .order_by(columns.sort_order.asc().nulls_first(), columns.created_at, columns.id)).mappings()]

    def update_goal(self, subject_id: str, values: dict) -> None:
        if values:
            self.connection.execute(update(t.subjects).where(
                t.subjects.c.user_id == self.user_id, t.subjects.c.id == subject_id,
                t.subjects.c.status == "draft").values(**values))

    def append_messages(self, subject_id: str | None, messages: list[dict], at: datetime) -> None:
        if not messages:
            return
        last = self.connection.scalar(select(func.max(t.onboarding_messages.c.sort_order))
                                      .where(*self._context(subject_id))) or 0
        for offset, message in enumerate(messages, 1):
            self.connection.execute(t.onboarding_messages.insert().values(
                id=str(uuid4()), user_id=self.user_id, subject_id=subject_id,
                sort_order=last + offset, created_at=at, **message))

    def events(self, subject_ids: list[str], *, include_global: bool) -> list[dict]:
        scope = t.calendar_events.c.subject_id.in_(subject_ids)
        if include_global:
            scope = or_(scope, t.calendar_events.c.subject_id.is_(None))
        return [dict(row) for row in self.connection.execute(select(t.calendar_events).where(
            t.calendar_events.c.user_id == self.user_id, t.calendar_events.c.deleted_at.is_(None),
            t.calendar_events.c.event_type.in_(("class", "job", "meet", "other")), scope)
            .order_by(t.calendar_events.c.starts_at, t.calendar_events.c.id)).mappings()]
