"""Initial onboarding calendar projection; approved schedule changes belong to C tasks."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

from app.core.clock import today_for
from app.domain.onboarding import event_view
from app.domain.scheduling import BusyInterval, free_slots
from app.repositories.events import EventRepository


def _local(value: datetime, tz: ZoneInfo):
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(tz)


def _minute(value: datetime):
    return Fraction(value.hour * 60 + value.minute) + Fraction(value.second * 1_000_000 + value.microsecond, 60_000_000)


def _span_intervals(start, end, days):
    for day in days:
        if start.date() <= day <= end.date():
            left = _minute(start) if start.date() == day else Fraction(0)
            right = _minute(end) if end.date() == day else Fraction(24 * 60)
            if left < right:
                yield BusyInterval(day, left, right)


def event_intervals(row, exceptions, week_start: date, timezone_name: str):
    """Project real local occurrences, including moves from outside this week."""
    tz = ZoneInfo(timezone_name)
    start, end = _local(row["starts_at"], tz), _local(row["ends_at"], tz)
    if row["ends_at"] <= row["starts_at"]:
        raise ValueError("Stored event ends before it starts")
    view = event_view(row, timezone_name)
    if row["recurrence_rule"]:
        rule = dict(part.split("=", 1) for part in row["recurrence_rule"].split(";"))
        if set(rule) != {"FREQ", "BYDAY"}:
            raise ValueError("Unsupported stored weekly recurrence modifiers")
    days = tuple(week_start + timedelta(days=offset) for offset in range(7))
    first, until, weekdays = start.date(), row["recurrence_until"], view["days"]
    def occurs(day):
        return day == first if weekdays is None else day >= first and (until is None or day <= until) and day.isoweekday() in weekdays
    changes = {}
    for exception in exceptions:
        if exception["event_id"] != row["id"] or exception["occurrence_date"] in changes:
            raise ValueError("Exceptions must belong to unique event occurrences")
        if exception["kind"] not in ("cancelled", "moved"):
            raise ValueError("Unknown stored occurrence exception")
        changes[exception["occurrence_date"]] = exception
    intervals = []
    if weekdays is None:
        if first not in changes:
            intervals.extend(_span_intervals(start, end, days))
    else:
        duration_days = (end.date() - start.date()).days
        # New onboarding events never cross midnight. This also reads existing
        # multi-day rows without losing their overlap at the week boundary.
        earliest = max(first, week_start - timedelta(days=duration_days))
        for offset in range(max(0, (days[-1] - earliest).days + 1)):
            original = earliest + timedelta(days=offset)
            if occurs(original) and original not in changes:
                occurrence_start = datetime.combine(original, start.timetz())
                occurrence_end = datetime.combine(original + timedelta(days=duration_days), end.timetz())
                intervals.extend(_span_intervals(occurrence_start, occurrence_end, days))
    for original, exception in changes.items():
        if not occurs(original) or exception["kind"] == "cancelled":
            continue
        moved_start, moved_end = exception["new_starts_at"], exception["new_ends_at"]
        if moved_start is None or moved_end is None or moved_end <= moved_start:
            raise ValueError("A moved occurrence needs a valid stored interval")
        intervals.extend(_span_intervals(_local(moved_start, tz), _local(moved_end, tz), days))
    return tuple(intervals)


class OnboardingEventsService:
    def __init__(self, connection, user):
        self.connection, self.user = connection, user
        self.repository = EventRepository(connection, user["id"])
        self.today = today_for(user)

    def calendar(self):
        week_start = self.today - timedelta(days=self.today.weekday())
        rows = self.repository.events()
        by_event = defaultdict(list)
        for exception in self.repository.exceptions([row["id"] for row in rows]):
            by_event[exception["event_id"]].append(exception)
        intervals = tuple(interval for row in rows for interval in event_intervals(
            row, by_event[row["id"]], week_start, self.user["timezone"]))
        free = sum((slot.minutes for offset in range(7)
                    for slot in free_slots(week_start + timedelta(days=offset), intervals)), Fraction())
        def number(value):
            return value.numerator if value.denominator == 1 else float(value)
        return {"weekStart": week_start.isoformat(), "today": self.today.isoformat(),
                "events": [event_view(row, self.user["timezone"]) for row in rows], "exams": self.repository.exams(),
                "summary": {"fixedMinutes": number(Fraction(6300) - free), "freeMinutes": number(free)}, "addedId": None}
