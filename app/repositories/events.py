"""Calendar rows owned through both event.user_id and optional subject ownership."""
from uuid import uuid4

from sqlalchemy import Connection, or_, select, update

from app.db import schema as t


class EventRepository:
    def __init__(self, connection: Connection, user_id: str):
        self.connection, self.user_id = connection, user_id

    def _owned(self):
        subjects = select(t.subjects.c.id).where(t.subjects.c.user_id == self.user_id)
        return (t.calendar_events.c.user_id == self.user_id, t.calendar_events.c.deleted_at.is_(None),
                t.calendar_events.c.event_type.in_(("class", "job", "meet", "other")),
                or_(t.calendar_events.c.subject_id.is_(None), t.calendar_events.c.subject_id.in_(subjects)))

    def events(self):
        return [dict(row) for row in self.connection.execute(select(t.calendar_events).where(*self._owned())
            .order_by(t.calendar_events.c.starts_at, t.calendar_events.c.id)).mappings()]

    def event(self, event_id: str):
        row = self.connection.execute(select(t.calendar_events).where(*self._owned(), t.calendar_events.c.id == event_id)).mappings().first()
        return dict(row) if row else None

    def exceptions(self, event_ids):
        return [dict(row) for row in self.connection.execute(select(t.calendar_event_exceptions)
            .join(t.calendar_events, t.calendar_event_exceptions.c.event_id == t.calendar_events.c.id)
            .where(*self._owned(), t.calendar_event_exceptions.c.event_id.in_(event_ids))
            .order_by(t.calendar_event_exceptions.c.occurrence_date, t.calendar_event_exceptions.c.id)).mappings()]

    def exams(self):
        return [{"subjectId": row["id"], "name": row["name"], "examDate": row["exam_date"]}
                for row in self.connection.execute(select(t.subjects.c.id, t.subjects.c.name, t.subjects.c.exam_date)
                    .where(t.subjects.c.user_id == self.user_id, t.subjects.c.status.in_(("draft", "active")),
                           t.subjects.c.exam_date.is_not(None))
                    .order_by(t.subjects.c.exam_date, t.subjects.c.id)).mappings()]

    def has_approved_plan(self):
        return self.connection.scalar(select(t.plans.c.id).where(t.plans.c.user_id == self.user_id)) is not None

    def create(self, values, at):
        event_id = str(uuid4())
        self.connection.execute(t.calendar_events.insert().values(
            id=event_id, user_id=self.user_id, subject_id=None, revision=1, created_at=at, updated_at=at, **values))
        return event_id

    def update(self, event_id, values, at):
        if not values:
            return True
        changed = self.connection.execute(update(t.calendar_events).where(*self._owned(), t.calendar_events.c.id == event_id)
            .values(**values, revision=t.calendar_events.c.revision + 1, updated_at=at))
        return changed.rowcount == 1

    def delete(self, event_id, at):
        # Keep exception/history rows; a deleted parent never contributes time.
        return self.update(event_id, {"deleted_at": at}, at)
