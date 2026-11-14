from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from app.core.responses import ApiError
from app.schemas.onboarding_events import EventCreate, EventPatch
from app.db import schema as t
from app.domain.user import User
from app.repositories.users import UserRepository
from app.services.onboarding_events import OnboardingEventsService, event_intervals


NOW = datetime(2026, 11, 11, 5, tzinfo=timezone.utc)
MONDAY = date(2026, 11, 9)


def at(day, clock, tz="Asia/Seoul"):
    return datetime.fromisoformat(f"{day.isoformat()}T{clock}").replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


@pytest.fixture
def events_user(domain_connection):
    user = replace(User.anonymous(now=NOW), demo_date=date(2026, 11, 11))
    UserRepository(domain_connection).add(user)
    domain_connection.commit()
    return user.to_mapping()


def add_event(conn, user, *, day=MONDAY, start="09:00", end="10:00", **changes):
    row = {"id": str(uuid4()), "user_id": user["id"], "subject_id": None, "event_type": "class", "title": "수업",
           "starts_at": at(day, start, user["timezone"]), "ends_at": at(day, end, user["timezone"]),
           "recurrence_rule": None, "recurrence_until": None, "source_text": None, "revision": 1, "deleted_at": None,
           "created_at": NOW, "updated_at": NOW, **changes}
    conn.execute(t.calendar_events.insert().values(**row))
    return row


def add_exception(conn, event, original, *, kind="cancelled", moved_day=None, start="10:00", end="12:00"):
    row = {"id": str(uuid4()), "event_id": event["id"], "occurrence_date": original, "kind": kind,
           "new_starts_at": at(moved_day, start) if moved_day else None,
           "new_ends_at": at(moved_day, end) if moved_day else None, "created_at": NOW, "updated_at": NOW}
    conn.execute(t.calendar_event_exceptions.insert().values(**row))
    return row


def payload(**changes):
    return {"kind": "class", "title": "수업", "days": [1, 3], "date": None, "start": "09:00", "end": "10:00", **changes}


def test_create_accepts_fe_nullable_discriminator_and_sorts_weekdays():
    assert EventCreate.model_validate(payload(days=[7, 1, 4])).days == [1, 4, 7]
    single = EventCreate.model_validate(payload(days=None, date="2026-11-14"))
    assert single.date == date(2026, 11, 14) and single.days is None


@pytest.mark.parametrize("changes", [{"days": None}, {"days": []}, {"days": [1, 1]}, {"days": [0]}, {"days": [8]},
    {"days": [True]}, {"days": [1.0]}, {"days": ["1"]}, {"days": "1"}, {"date": "2026-11-14"},
    {"days": None, "date": "2026-02-29"}, {"days": None, "date": "2026-1-01"},
    {"days": None, "date": 1794600000}, {"days": None, "date": "2026-11-14T00:00:00Z"},
    {"start": "9:00"}, {"start": "24:00"}, {"end": "23:60"}, {"start": "09:00:00"},
    {"start": "10:00"}, {"start": "23:00", "end": "01:00"}, {"start": 900}, {"kind": "exam"},
    {"title": 42}, {"title": None}, {"title": "a\x00b"}, {"title": "a\ud800b"}, {"title": "a\udfffb"},
    {"added": True}, {"subjectId": "id"}])
def test_invalid_create_values_are_rejected_without_coercion(changes):
    with pytest.raises(ValidationError):
        EventCreate.model_validate(payload(**changes))


@pytest.mark.parametrize("field", ["kind", "title", "start", "end"])
def test_create_requires_all_nonnullable_event_fields(field):
    data = payload()
    del data[field]
    with pytest.raises(ValidationError):
        EventCreate.model_validate(data)


@pytest.mark.parametrize("title", ["", "  \t\n"])
def test_blank_required_title_is_empty_400(title):
    with pytest.raises(ApiError) as error:
        EventCreate.model_validate(payload(title=title))
    assert error.value.code == "EMPTY" and error.value.status == 400


def test_patch_tracks_omission_and_explicit_nullable_switch():
    assert EventPatch.model_validate({}).model_dump(exclude_unset=True) == {}
    patch = EventPatch.model_validate({"days": None, "date": "2026-11-14"})
    assert patch.model_dump(exclude_unset=True) == {"days": None, "date": date(2026, 11, 14)}
    for key in ("kind", "title", "start", "end"):
        with pytest.raises(ValidationError):
            EventPatch.model_validate({key: None})


def test_empty_calendar_uses_full_local_week_and_read_has_no_side_effects(domain_connection, events_user):
    conn, user = domain_connection, events_user
    before = dict(conn.execute(select(t.users)).mappings().one())
    assert OnboardingEventsService(conn, user).calendar() == {
        "weekStart": "2026-11-09", "today": "2026-11-11", "events": [], "exams": [],
        "summary": {"fixedMinutes": 0, "freeMinutes": 6300}, "addedId": None}
    assert dict(conn.execute(select(t.users)).mappings().one()) == before
    assert conn.execute(select(t.plans)).first() is None


def test_calendar_clips_to_eight_through_twenty_three_and_subtracts_interval_union(domain_connection, events_user):
    conn, user = domain_connection, events_user
    for start, end in (("06:00", "09:00"), ("08:30", "09:30"), ("09:00", "10:00"),
                       ("22:00", "23:30"), ("00:00", "07:00"), ("23:15", "23:59")):
        add_event(conn, user, start=start, end=end)
    add_event(conn, user, day=MONDAY - timedelta(days=1), start="08:00", end="23:00")
    view = OnboardingEventsService(conn, user).calendar()
    assert view["summary"] == {"fixedMinutes": 180, "freeMinutes": 6120}
    assert len(view["events"]) == 7 and view["addedId"] is None


def test_recurring_partial_week_respects_first_occurrence_and_inclusive_until(domain_connection, events_user):
    conn, user = domain_connection, events_user
    add_event(conn, user, day=MONDAY + timedelta(days=2), recurrence_rule="FREQ=WEEKLY;BYDAY=FR,WE,MO",
              recurrence_until=MONDAY + timedelta(days=3))
    view = OnboardingEventsService(conn, user).calendar()
    assert view["summary"] == {"fixedMinutes": 60, "freeMinutes": 6240}
    assert view["events"][0]["days"] == [1, 3, 5] and view["events"][0]["date"] is None


def test_cancel_and_moved_occurrences_include_moves_from_outside_week_and_union_overlaps(domain_connection, events_user):
    conn, user = domain_connection, events_user
    event = add_event(conn, user, day=MONDAY - timedelta(days=5), recurrence_rule="FREQ=WEEKLY;BYDAY=MO,WE,FR",
                      recurrence_until=MONDAY + timedelta(days=4))
    add_exception(conn, event, MONDAY + timedelta(days=2))
    add_exception(conn, event, MONDAY - timedelta(days=3), kind="moved", moved_day=MONDAY + timedelta(days=1))
    add_exception(conn, event, MONDAY + timedelta(days=4), kind="moved", moved_day=MONDAY + timedelta(days=8))
    # These were never valid base occurrences and cannot manufacture busy time.
    add_exception(conn, event, MONDAY - timedelta(days=7), kind="moved", moved_day=MONDAY, start="12:00", end="13:00")
    add_exception(conn, event, MONDAY + timedelta(days=7), kind="moved", moved_day=MONDAY, start="12:00", end="13:00")
    add_event(conn, user, day=MONDAY + timedelta(days=1), start="11:00", end="13:00")
    assert OnboardingEventsService(conn, user).calendar()["summary"] == {"fixedMinutes": 240, "freeMinutes": 6060}


def test_one_off_cancellation_and_move_across_week_boundary(domain_connection, events_user):
    conn, user = domain_connection, events_user
    cancelled = add_event(conn, user)
    add_exception(conn, cancelled, MONDAY)
    moved = add_event(conn, user, day=MONDAY - timedelta(days=1))
    add_exception(conn, moved, MONDAY - timedelta(days=1), kind="moved", moved_day=MONDAY + timedelta(days=6))
    view = OnboardingEventsService(conn, user).calendar()
    assert view["summary"] == {"fixedMinutes": 120, "freeMinutes": 6180}
    assert len(view["events"]) == 2  # Original Event series remain editable, without invented occurrence IDs.


def test_all_day_overlap_can_fill_the_window_but_never_make_free_minutes_negative(domain_connection, events_user):
    for _ in range(2):
        add_event(domain_connection, events_user, start="00:00", end="23:59",
                  recurrence_rule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU")
    assert OnboardingEventsService(domain_connection, events_user).calendar()["summary"] == {
        "fixedMinutes": 6300, "freeMinutes": 0}


def add_subject(conn, user_id, *, exam_date=None, status="draft"):
    subject_id = str(uuid4())
    conn.execute(t.subjects.insert().values(id=subject_id, user_id=user_id, code=subject_id, name="시험 과목", color="#123456",
                                           status=status, exam_date=exam_date, created_at=NOW, updated_at=NOW))
    return subject_id


def test_read_filters_foreign_deleted_exam_events_and_foreign_subject_references(domain_connection, events_user):
    conn, user = domain_connection, events_user
    other = User.anonymous(now=NOW)
    UserRepository(conn).add(other)
    foreign_subject = add_subject(conn, other.id, exam_date=MONDAY)
    owned_subject = add_subject(conn, user["id"], exam_date=MONDAY + timedelta(days=10))
    add_subject(conn, user["id"], exam_date=MONDAY, status="archived")
    add_subject(conn, user["id"])
    owned = add_event(conn, user, subject_id=owned_subject)
    add_event(conn, other.to_mapping())
    add_event(conn, user, subject_id=foreign_subject)
    add_event(conn, user, deleted_at=NOW)
    add_event(conn, user, event_type="exam")
    view = OnboardingEventsService(conn, user).calendar()
    assert [event["id"] for event in view["events"]] == [owned["id"]]
    assert view["exams"] == [{"subjectId": owned_subject, "name": "시험 과목", "examDate": MONDAY + timedelta(days=10)}]
    assert view["summary"]["fixedMinutes"] == 60


def test_approved_calendar_read_and_new_draft_subject_do_not_change_existing_events_or_plan(domain_connection, events_user):
    conn, user = domain_connection, events_user
    add_event(conn, user)
    plan_id = str(uuid4())
    conn.execute(t.plans.insert().values(id=plan_id, user_id=user["id"], revision=7, created_at=NOW, approved_at=NOW))
    conn.execute(update(t.users).where(t.users.c.id == user["id"]).values(onboarding_status="completed"))
    before = dict(conn.execute(select(t.calendar_events)).mappings().one())
    completed = {**user, "onboarding_status": "completed"}
    first = OnboardingEventsService(conn, completed).calendar()
    add_subject(conn, user["id"], exam_date=MONDAY + timedelta(days=20))
    second = OnboardingEventsService(conn, completed).calendar()
    assert first["events"] == second["events"] and first["summary"] == second["summary"]
    assert dict(conn.execute(select(t.calendar_events)).mappings().one()) == before
    assert conn.scalar(select(t.plans.c.revision)) == 7


def test_stored_utc_dates_are_projected_in_user_timezone_not_database_date(domain_connection, events_user):
    conn, user = domain_connection, events_user
    add_event(conn, user, day=MONDAY, start="00:30", end="01:30")
    row = OnboardingEventsService(conn, user).calendar()["events"][0]
    assert row["date"] == MONDAY.isoformat() and row["start"] == "00:30"
    stored = conn.execute(select(t.calendar_events)).mappings().one()
    assert stored["starts_at"].date() == MONDAY - timedelta(days=1)
