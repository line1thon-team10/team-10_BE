from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_focus_state import clock, headers


@pytest.mark.parametrize("seconds,expected",[(1079,409),(1080,200),(1081,200)])
def test_next_day_settles_expired_focus_without_a_prior_focus_get(api_client,clock,demo_user,domain_connection,seconds,expected):
    clock[0] += timedelta(seconds=seconds)
    response = api_client.post("/api/demo/next-day",headers=headers(demo_user))
    assert response.status_code==expected,response.text
    row=domain_connection.execute(select(t.study_sessions)).mappings().one()
    if expected==200:
        assert row["status"]=="auto_stopped" and row["measured_seconds"]==3000
        assert response.json()["data"]["today"]=="2026-11-10"
    else:
        assert response.json()["error"]["code"]=="SESSION_RUNNING"
        assert row["status"]=="running"
