"""Independent HTTP requests exercise leased turns and response isolation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.db import schema as t
from tests.test_onboarding_dialogue_e2e import dialogue_case, message, TEXT
from tests.test_onboarding_voice_e2e import voice, result, TEXT as VOICE_TEXT


def block_first(model):
    entered, release, mutex = Event(), Event(), Lock()
    count = 0
    def callback():
        nonlocal count
        with mutex:
            count += 1
            first = count == 1
        if first:
            entered.set()
            assert release.wait(10), 'test did not release provider boundary'
    model.callback = callback
    return entered, release


@pytest.mark.parametrize('expired', [False, True])
def test_same_key_concurrency_and_expired_lease_fence_late_response(api_client, domain_connection, dialogue_case, expired):
    case, conn = dialogue_case, domain_connection
    entered, release = block_first(case['model'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(message, api_client, case)
        try:
            assert entered.wait(5)
            if expired:
                conn.execute(update(t.ai_jobs).where(t.ai_jobs.c.user_id == case['user']).values(
                    started_at=datetime.now(timezone.utc)-timedelta(days=1)))
                conn.commit()
            second = message(api_client, case)
            assert second.status_code == (200 if expired else 409), second.text
            if not expired:
                assert second.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
        finally:
            release.set()
        first = first.result(timeout=5)
    assert first.status_code == (409 if expired else 200), first.text
    if expired:
        assert first.json()['error']['code'] == 'INPUT_STALE'
    successful = second if expired else first
    replay = message(api_client, case)
    assert replay.status_code == 200 and replay.json() == successful.json()
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 2
    job = conn.execute(select(t.ai_jobs).where(t.ai_jobs.c.user_id == case['user'])).mappings().one()
    assert job['status'] == 'succeeded' and job['retry_count'] == (2 if expired else 1)
    assert case['model'].calls == (2 if expired else 1)


def test_different_keys_capture_same_basis_but_only_one_turn_applies(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    entered, release = block_first(case['model'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(message, api_client, case)
        try:
            assert entered.wait(5)
            winner = message(api_client, case, key=str(uuid4()))
            assert winner.status_code == 200, winner.text
        finally:
            release.set()
        late = late.result(timeout=5)
    assert late.status_code == 409 and late.json()['error']['code'] == 'INPUT_STALE', late.text
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 2
    assert conn.scalar(select(func.count()).select_from(t.ai_jobs).where(t.ai_jobs.c.status == 'succeeded')) == 1


def test_uuid_key_is_isolated_by_owner_and_path(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    key = 'abcdefab-cdef-4abc-8def-abcdefabcdef'
    goals = message(api_client, case, key=key)
    assert goals.status_code == 200, goals.text
    case['model'].transform = lambda *_: result()
    calendar = voice(api_client, case, key=key.upper().replace('-', ''))
    assert calendar.status_code == 200, calendar.text
    assert voice(api_client, case, key=key).json() == calendar.json()
    assert message(api_client, case, key=key.upper()).json() == goals.json()
    other = api_client.post('/api/session').json()['data']['anonymousId']
    other_headers = {'X-Anonymous-Id': other}
    response = api_client.post('/api/subjects', headers=other_headers, json={'name': '다른 사용자'})
    assert response.status_code == 200
    conn.execute(update(t.users).where(t.users.c.id == other).values(demo_date=datetime(2026, 11, 16).date()))
    conn.commit()
    case['model'].transform = None
    other_response = message(api_client, {**case, 'headers': other_headers}, key=key)
    assert other_response.status_code == 200, other_response.text
    assert other_response.json()['data']['subjects'][0]['id'] != case['subject']
    assert conn.scalar(select(func.count()).select_from(t.ai_jobs)) == 3
    assert case['model'].calls == 3


def test_local_day_change_after_ai_rejects_without_turn_write(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    def advance():
        with case['model'].engine.begin() as writer:
            writer.execute(update(t.users).where(t.users.c.id == case['user']).values(demo_date=datetime(2026, 11, 17).date()))
    case['model'].callback = advance
    response = message(api_client, case)
    assert response.status_code == 409 and response.json()['error']['code'] == 'INPUT_STALE', response.text
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0
    assert conn.scalar(select(t.subjects.c.exam_date).where(t.subjects.c.id == case['subject'])) is None


def test_final_replay_persistence_failure_rolls_back_entire_turn(api_client, domain_connection, dialogue_case):
    case, conn = dialogue_case, domain_connection
    def fail(db_conn, cursor, statement, params, context, many):
        if context.isupdate and getattr(context.compiled.statement.table, 'name', None) == 'ai_jobs':
            compiled = context.compiled_parameters
            if any(values.get('status') == 'succeeded' for values in compiled):
                raise RuntimeError('cannot persist replay envelope')
    event.listen(case['model'].engine, 'before_cursor_execute', fail)
    try:
        failed = message(api_client, case)
        assert failed.status_code == 500, failed.text
    finally:
        event.remove(case['model'].engine, 'before_cursor_execute', fail)
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 0
    assert conn.scalar(select(t.subjects.c.target_score).where(t.subjects.c.id == case['subject'])) is None
    conn.commit()
    retried = message(api_client, case)
    assert retried.status_code == 200, retried.text
    assert message(api_client, case).json() == retried.json()
    assert conn.scalar(select(func.count()).select_from(t.onboarding_messages)) == 2
