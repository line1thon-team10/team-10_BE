"""Schedule candidates must respect source intent and exact occupied instants."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_plan_draft import plan_case
from tests.test_plan_addition_e2e import addition_case
from tests.e2e.test_schedule_voice import voice_case, preview as voice_preview
from tests.e2e.test_schedule_edit import calendar_world, preview as edit_preview
from tests.e2e.test_schedule_delete import user
from tests.test_onboarding_events import add_event, at


@pytest.mark.parametrize('suffix', ['약속은 취소됐어', '약속은 없으니 추가하지 마'])
def test_explicitly_cancelled_or_denied_appointment_does_not_create_addition_preview(api_client, domain_connection, voice_case, suffix):
    case, conn = voice_case, domain_connection
    before = conn.execute(select(t.calendar_events)).all()
    conn.commit()
    text = case['text'].replace('친구 약속', suffix)
    response = voice_preview(api_client, case, text)
    assert response.status_code == 400 and response.json()['error']['code'] == 'PARSE_FAILED', response.text
    assert conn.execute(select(t.calendar_events)).all() == before
    assert conn.execute(select(t.plan_proposals).where(t.plan_proposals.c.proposal_type == 'schedule')).all() == []


def test_move_cannot_overlap_last_seconds_of_existing_fixed_event(api_client, calendar_world):
    world = calendar_world
    edited = add_event(world.conn, user(world), day=world.day, start='16:00', end='17:00')
    add_event(world.conn, user(world), day=world.day, start='12:00', end='13:00',
        ends_at=at(world.day, '13:00') + timedelta(seconds=30))
    before = world.conn.execute(select(t.calendar_events)).all()
    world.conn.commit()
    response = edit_preview(api_client, world.user, f"{edited['id']}@{world.day}",
        {'date': world.day.isoformat(), 'start': '13:00', 'end': '14:00'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'SCHEDULE_CONFLICT', response.text
    assert world.conn.execute(select(t.calendar_events)).all() == before
