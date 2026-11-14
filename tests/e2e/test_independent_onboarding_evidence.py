"""A model's confidence is not evidence that the user provided date/time facts."""
import pytest
from sqlalchemy import func, select

from app.db import schema as t
from tests.test_onboarding_dialogue_e2e import dialogue_case, message
from tests.test_onboarding_voice_e2e import voice, result, EVENT


@pytest.mark.parametrize('text,event', [
    ('토요일 저녁에 친구 약속', {**EVENT, 'kind': 'meet', 'title': '친구 약속', 'days': [6], 'start': '18:00', 'end': '20:00'}),
    ('2026-11-20 10:00부터 11:00까지 수업', {**EVENT, 'days': None, 'date': '2026-11-21', 'start': '10:00', 'end': '11:00'}),
    ('매주 월요일 10:00부터 11:00까지 수업', {**EVENT, 'days': [2], 'start': '10:00', 'end': '11:00'}),
])
def test_voice_refuses_invented_or_contradictory_schedule(api_client, domain_connection, dialogue_case, text, event):
    case, conn = dialogue_case, domain_connection
    case['model'].transform = lambda *_: result(event)
    response = voice(api_client, case, text=text)
    assert response.status_code == 400 and response.json()['error']['code'] == 'PARSE_FAILED', response.text
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0


def test_goal_only_text_cannot_confirm_unmentioned_optional_groups(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    # Fixture quotes the exact text for every group, which is insufficient when
    # that text says nothing about either optional group's explicit absence.
    response = message(api_client, case, text='시험은 2026-11-20이고 목표는 A+')
    assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    subject = conn.execute(select(t.subjects).where(t.subjects.c.id == case['subject'])).mappings().one()
    assert subject['exam_date'] is None
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0
