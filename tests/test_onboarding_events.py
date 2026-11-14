from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.core.responses import ApiError
from app.schemas.onboarding_events import EventCreate, EventPatch


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
