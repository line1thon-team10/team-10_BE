"""Natural-language schedule HTTP writes use the existing calendar validator."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.db import schema as t
from tests.test_onboarding_dialogue_e2e import dialogue_case

TEXT = '매주 월요일과 수요일 10:00부터 11:30까지 자료구조 수업'
EVENT = {'kind': 'class', 'title': '자료구조 수업', 'days': [1, 3], 'date': None, 'start': '10:00', 'end': '11:30'}


def voice(client, case, *, text=TEXT, key='11111111-1111-4111-8111-111111111111'):
    return client.post('/api/onboarding/events/voice', headers={**case['headers'], 'Idempotency-Key': key}, json={'text': text})


def result(event=EVENT, confirmed=True):
    return {'message': '일정을 확인했어요', 'chips': [], 'isConfirmed': confirmed, 'goals': [], 'event': deepcopy(event)}


def test_voice_adds_exact_weekly_event_and_replays_added_id(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    case['model'].transform = lambda *_: result()
    first = voice(api_client, case)
    assert first.status_code == 200, first.text
    value = first.json()['data']
    added = value['addedId']
    assert value['events'] == [{'id': added, **EVENT, 'added': True}]
    assert value['summary'] == {'fixedMinutes': 180, 'freeMinutes': 6120}
    assert voice(api_client, case).json() == first.json()
    assert case['model'].calls == 1
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 1
    assert conn.scalar(select(t.calendar_events.c.recurrence_rule)) == 'FREQ=WEEKLY;BYDAY=MO,WE'
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0


def test_ambiguous_voice_has_no_schedule_write_and_can_retry(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    case['model'].transform = lambda *_: result(None, False)
    response = voice(api_client, case, text='토요일 저녁에 친구 약속')
    assert response.status_code == 400 and response.json()['error']['code'] == 'PARSE_FAILED', response.text
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0
    conn.commit()
    case['model'].transform = lambda *_: result()
    retry = voice(api_client, case, text='토요일 저녁에 친구 약속')
    assert retry.status_code == 400 and retry.json()['error']['code'] == 'PARSE_FAILED'
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0
    conn.commit()
    exact = voice(api_client, case, key='22222222-2222-4222-8222-222222222222')
    assert exact.status_code == 200 and exact.json()['data']['addedId'] is not None


@pytest.mark.parametrize('bad', ['midnight', 'duplicate_day', 'two_date_modes', 'unknown_kind', 'goal_mutation'])
def test_invalid_voice_result_never_mutates_schedule_or_subjects(api_client, domain_connection, dialogue_case, bad):
    case, conn = dialogue_case, domain_connection
    def transform(value, context):
        if bad == 'goal_mutation':
            value['event'] = deepcopy(EVENT)
            return value
        value = result()
        if bad == 'midnight':
            value['event'].update(start='23:00', end='01:00')
        elif bad == 'duplicate_day':
            value['event']['days'] = [1, 1]
        elif bad == 'two_date_modes':
            value['event']['date'] = '2026-11-17'
        else:
            value['event']['kind'] = 'private'
        return value
    case['model'].transform = transform
    response = voice(api_client, case)
    assert response.status_code == 503 and response.json()['error']['code'] == 'AI_FAILED', response.text
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id == case['subject'])) is None


def test_plan_approval_while_parsing_prevents_late_schedule_write(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    case['model'].transform = lambda *_: result()
    def approve():
        with case['model'].engine.begin() as writer:
            now = datetime.now(timezone.utc)
            writer.execute(t.plans.insert().values(id=str(uuid4()), user_id=case['user'], revision=1,
                created_at=now, approved_at=now))
    case['model'].callback = approve
    response = voice(api_client, case)
    assert response.status_code == 409 and response.json()['error']['code'] == 'INPUT_STALE', response.text
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0
    conn.commit()
    case['model'].callback = None
    refused = voice(api_client, case, key='22222222-2222-4222-8222-222222222222')
    assert refused.status_code == 409 and refused.json()['error']['code'] == 'ONBOARDING_COMPLETED'
    assert case['model'].calls == 1


def test_nonexistent_dst_time_is_parse_failed_without_write(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    conn.execute(update(t.users).where(t.users.c.id == case['user']).values(timezone='America/New_York'))
    conn.commit()
    case['model'].transform = lambda *_: result({**EVENT, 'days': None, 'date': '2027-03-14', 'start': '02:30', 'end': '03:30'})
    response = voice(api_client, case, text='2027-03-14 02:30부터 03:30까지 수업')
    assert response.status_code == 400 and response.json()['error']['code'] == 'PARSE_FAILED', response.text
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0


def test_voice_retry_after_calendar_response_failure_has_no_duplicate_event(api_client, domain_connection, dialogue_case, monkeypatch):
    from app.services.onboarding_events import OnboardingEventsService
    case, conn = dialogue_case, domain_connection
    case['model'].transform = lambda *_: result()
    original = OnboardingEventsService.calendar
    def failure(self):
        original(self)
        raise RuntimeError('calendar response failed')
    monkeypatch.setattr(OnboardingEventsService, 'calendar', failure)
    response = voice(api_client, case)
    assert response.status_code == 500
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0
    conn.commit()
    monkeypatch.setattr(OnboardingEventsService, 'calendar', original)
    response = voice(api_client, case)
    assert response.status_code == 200
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 1


@pytest.mark.parametrize('text,event', [
    ('토요일 저녁에 친구 약속', {**EVENT, 'days': [6], 'start': '18:00', 'end': '20:00'}),
    ('2026-11-20 10:00부터 11:00까지 수업', {**EVENT, 'days': None, 'date': '2026-11-21', 'end': '11:00'}),
    ('매주 월요일 10:00부터 11:00까지 수업', {**EVENT, 'days': [2], 'end': '11:00'}),
    ('매주 월요일 오전 10시부터 11시까지 수업', {**EVENT, 'days': [1], 'start': '22:00', 'end': '23:00'}),
    ('2026년 11월 20일 월요일 10시부터 11시까지 수업', {**EVENT, 'days': None, 'date': '2026-11-20', 'end': '11:00'}),
    ('매주 월요일 10시부터 11시까지, 수요일 13시부터 14시까지 수업', EVENT),
    ('매주 월요일 100시부터 101시까지 수업', {**EVENT, 'days': [1], 'start': '00:00', 'end': '01:00'}),
    ('매주 월요일 스물한 시부터 스물두 시까지 수업', {**EVENT, 'days': [1], 'start': '01:00', 'end': '02:00'}),
])
def test_source_contradiction_or_missing_coordinates_never_writes(api_client, domain_connection, dialogue_case, text, event):
    case, conn = dialogue_case, domain_connection
    case['model'].transform = lambda *_: result(event)
    response = voice(api_client, case, text=text)
    assert response.status_code == 400 and response.json()['error']['code'] == 'PARSE_FAILED', response.text
    assert conn.scalar(select(func.count()).select_from(t.calendar_events)) == 0
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0


@pytest.mark.parametrize('text,event', [
    ('매주 월요일 오후 두 시부터 세 시 반까지 수업', {**EVENT, 'days': [1], 'start': '14:00', 'end': '15:30'}),
    ('매주 수요일 오전 10시 15분부터 오후 1시까지 수업', {**EVENT, 'days': [3], 'start': '10:15', 'end': '13:00'}),
    ('2026년 11월 20일 금요일 오전 열 시부터 열한 시 반까지 수업', {**EVENT, 'days': None, 'date': '2026-11-20'}),
    ('내일 10:00부터 11:30까지 수업', {**EVENT, 'days': None, 'date': '2026-11-17'}),
    ('모레 10시부터 11시 30분까지 수업', {**EVENT, 'days': None, 'date': '2026-11-18'}),
    ('매일 정오부터 오후 1시까지 점심', {**EVENT, 'days': list(range(1, 8)), 'start': '12:00', 'end': '13:00'}),
])
def test_explicit_korean_and_numeric_coordinates_can_be_saved(api_client, dialogue_case, text, event):
    case = dialogue_case
    case['model'].transform = lambda *_: result(event)
    response = voice(api_client, case, text=text)
    assert response.status_code == 200, response.text
    assert response.json()['data']['events'] == [{'id': response.json()['data']['addedId'], **event, 'added': True}]
