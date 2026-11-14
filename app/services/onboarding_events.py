"""Initial onboarding calendar projection; approved schedule changes belong to C tasks."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.core.clock import now_utc, today_for
from app.core.responses import ApiError
from app.domain.onboarding import event_view
from app.domain.scheduling import BusyInterval, free_slots
from app.repositories.events import EventRepository
from app.repositories.locking import UserBusyError, lock_user
from app.schemas.onboarding_events import EventCreate, EventPatch


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


def _local_time_to_utc(day: date, clock: str, timezone_name: str):
    """Reject missing/ambiguous wall times instead of silently choosing a DST fold."""
    tz, naive = ZoneInfo(timezone_name), datetime.combine(day, time.fromisoformat(clock))
    possible = set()
    for fold in (0, 1):
        try:
            instant = naive.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
            if instant.astimezone(tz).replace(tzinfo=None) == naive:
                possible.add(instant)
        except (OverflowError, ValueError):
            continue
    if len(possible) != 1:
        raise ApiError("INVALID_INPUT", "사용자 시간대에서 확정할 수 없는 날짜 또는 시각이에요", 422)
    return possible.pop()


class OnboardingEventsService:
    def __init__(self, connection, user):
        self.connection, self.user = connection, user
        self.repository = EventRepository(connection, user["id"])
        self.today = None

    def calendar(self):
        if self.today is None:
            self.today = today_for(self.user)
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

    def _lock(self):
        try:
            fresh = lock_user(self.connection, UUID(self.user["id"]))
        except UserBusyError as exc:
            raise ApiError("REQUEST_IN_PROGRESS", "다른 요청이 처리 중이에요. 잠시 후 다시 시도해 주세요", 409) from exc
        if fresh is None:
            raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
        self.user = fresh.to_mapping()
        self.today = today_for(self.user)

    def _initial_draft(self):
        if self.user["onboarding_status"] != "draft" or self.repository.has_approved_plan():
            raise ApiError("ONBOARDING_COMPLETED", "초기 온보딩이 끝났어요. 일정 관리에서 변경해 주세요", 409)

    def _event(self, event_id):
        try:
            event_id = str(UUID(str(event_id)))
        except (TypeError, ValueError, AttributeError):
            raise ApiError("EVENT_NOT_FOUND", "일정을 찾지 못했어요", 404) from None
        row = self.repository.event(event_id)
        if row is None:
            raise ApiError("EVENT_NOT_FOUND", "일정을 찾지 못했어요", 404)
        return row

    def _values(self, command: EventCreate, existing=None):
        values = {"event_type": command.kind, "title": command.title}
        old = event_view(existing, self.user["timezone"]) if existing else None
        old_date = date.fromisoformat(old["date"]) if old and old["date"] else None
        same_schedule = bool(old and old["days"] == command.days and old_date == command.date)
        def clock_for(field, clock_field):
            clock = getattr(command, clock_field)
            if old and old[clock_field] == clock:
                return _local(existing[field], ZoneInfo(self.user["timezone"])).time().isoformat()
            return clock
        if same_schedule:
            # Keep actual stored instants/first occurrence/until/exception rows on
            # partial edits, including legacy sub-minute timestamp precision.
            for field, clock_field in (("starts_at", "start"), ("ends_at", "end")):
                clock = getattr(command, clock_field)
                values[field] = existing[field] if old[clock_field] == clock else _local_time_to_utc(
                    _local(existing[field], ZoneInfo(self.user["timezone"])).date(), clock, self.user["timezone"])
            values.update(recurrence_rule=existing["recurrence_rule"], recurrence_until=existing["recurrence_until"])
        else:
            if command.days:
                first = (_local(existing["starts_at"], ZoneInfo(self.user["timezone"])).date()
                         if existing and existing["recurrence_rule"] else self.today - timedelta(days=self.today.weekday()))
                while first.isoweekday() not in command.days:
                    first += timedelta(days=1)
                names = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
                recurrence = "FREQ=WEEKLY;BYDAY=" + ",".join(names[day - 1] for day in command.days)
                until = existing["recurrence_until"] if existing and existing["recurrence_rule"] else None
            else:
                first, recurrence, until = command.date, None, None
            values.update(starts_at=_local_time_to_utc(first, clock_for("starts_at", "start"), self.user["timezone"]),
                ends_at=_local_time_to_utc(first, clock_for("ends_at", "end"), self.user["timezone"]),
                recurrence_rule=recurrence, recurrence_until=until)
        if _local(values["ends_at"], timezone.utc) <= _local(values["starts_at"], timezone.utc):
            raise ApiError("INVALID_INPUT", "일정의 종료는 시작보다 뒤여야 해요", 422)
        changed_schedule = existing is None or any(existing[key] != values[key]
            for key in ("starts_at", "ends_at", "recurrence_rule", "recurrence_until"))
        if changed_schedule:
            tz = ZoneInfo(self.user["timezone"])
            if _local(values["starts_at"], tz).date() != _local(values["ends_at"], tz).date():
                raise ApiError("INVALID_INPUT", "자정을 넘는 일정은 날짜별로 나누어 주세요", 422)
        return values

    def create(self, command: EventCreate):
        self._lock()
        self._initial_draft()
        self.repository.create(self._values(command), now_utc())
        return self.calendar()

    def patch(self, event_id, command: EventPatch):
        self._lock()
        row = self._event(event_id)
        self._initial_draft()
        current = event_view(row, self.user["timezone"])
        merged = {key: current[key] for key in EventCreate.model_fields}
        merged.update(command.model_dump(exclude_unset=True))
        try:
            valid = EventCreate.model_validate(merged, context={"validate_stored_instants": True})
        except ValidationError as exc:
            raise ApiError("INVALID_INPUT", "변경한 일정의 날짜·요일·시각을 확인해 주세요", 422) from exc
        changes = {key: value for key, value in self._values(valid, row).items() if row[key] != value}
        if not self.repository.update(row["id"], changes, now_utc()):
            raise ApiError("EVENT_NOT_FOUND", "일정을 찾지 못했어요", 404)
        return self.calendar()

    def delete(self, event_id):
        self._lock()
        row = self._event(event_id)
        self._initial_draft()
        if not self.repository.delete(row["id"], now_utc()):
            raise ApiError("EVENT_NOT_FOUND", "일정을 찾지 못했어요", 404)
        return self.calendar()
