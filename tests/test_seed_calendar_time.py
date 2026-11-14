"""Seed UTC storage regression, separate from new onboarding calendar behavior."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import schema as t
from app.seed import demo_data


def test_day9_seed_events_keep_the_same_korean_times_through_actual_get(api_client, demo_user, domain_connection):
    response = api_client.get("/api/onboarding", headers={"X-Anonymous-Id": demo_user})
    assert response.status_code == 200, response.text
    expected = {(title, "FREQ=WEEKLY;BYDAY=" + days): (kind, start, end)
                for kind, title, days, start, end in demo_data.EVENTS}
    weekdays = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
    events = response.json()["data"]["schedules"]
    assert {(row["title"], "FREQ=WEEKLY;BYDAY=" + ",".join(weekdays[day - 1] for day in row["days"])):
            (row["kind"], row["start"], row["end"]) for row in events} == expected
    for row in domain_connection.execute(select(t.calendar_events).where(t.calendar_events.c.user_id == demo_user)).mappings():
        for column, position in (("starts_at", 1), ("ends_at", 2)):
            stored = row[column]
            instant = stored if stored.tzinfo else stored.replace(tzinfo=timezone.utc)
            local = instant.astimezone(ZoneInfo("Asia/Seoul"))
            assert local.strftime("%H:%M") == expected[(row["title"], row["recurrence_rule"])][position]
            expected_utc = datetime.combine(local.date(), local.time(), ZoneInfo("Asia/Seoul")).astimezone(timezone.utc)
            assert instant == expected_utc
