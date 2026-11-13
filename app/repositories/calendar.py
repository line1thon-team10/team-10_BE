"""calendar persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class CalendarEventsRepository(Repository):
    table = schema.calendar_events


class CalendarEventExceptionsRepository(Repository):
    table = schema.calendar_event_exceptions
