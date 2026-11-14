"""Independent HTTP day-summary races and user-local temporal boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select, update

from app.db import schema as t
from tests.test_progress_contract import progress_world


def set_local_clock(monkeypatch, zone, day, hour, minute, second=0):
    from app.routers import today
    at = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    monkeypatch.setattr(today, 'now_utc', lambda: at)


def get_today(client, world):
    return client.get('/api/today', headers={'X-Anonymous-Id': world.user})


def notices(world):
    return world.conn.execute(select(t.user_notices).where(t.user_notices.c.user_id == world.user,
        t.user_notices.c.notice_type == 'day_summary').order_by(t.user_notices.c.notice_date)).mappings().all()


@pytest.mark.parametrize('zone', ['Asia/Seoul', 'America/New_York', 'Pacific/Kiritimati'])
def test_actual_timezone_2330_and_new_day_have_distinct_admission(api_client, progress_world, monkeypatch, zone):
    w = progress_world
    unit = w.unit(1, 100)
    w.task((unit, 1, 70), done=[(unit, 1, 70)], status='completed')
    w.task((unit, 71, 100))
    w.conn.execute(update(t.users).where(t.users.c.id == w.user).values(demo_date=None, timezone=zone))
    w.conn.commit()
    set_local_clock(monkeypatch, zone, w.day, 23, 29, 59)
    before = get_today(api_client, w)
    assert before.status_code == 200, before.text
    assert before.json()['data']['date'] == str(w.day) and before.json()['data']['event'] is None
    set_local_clock(monkeypatch, zone, w.day, 23, 30)
    at_boundary = get_today(api_client, w).json()['data']
    assert at_boundary['event']['reason'] == 'dayClose' and at_boundary['event']['rate'] == 70
    assert get_today(api_client, w).json()['data']['event'] is None
    tomorrow = w.day + timedelta(days=1)
    w.task((unit, 1, 100), done=[(unit, 1, 100)], status='completed', day=tomorrow)
    w.conn.commit()
    set_local_clock(monkeypatch, zone, tomorrow, 0, 0)
    next_day = get_today(api_client, w).json()['data']
    assert next_day['date'] == str(tomorrow) and next_day['event']['reason'] == 'dayClose'
    assert [row['notice_date'] for row in notices(w)] == [w.day, tomorrow]


def test_display_seventy_does_not_admit_until_raw_threshold_after_correction(api_client, progress_world, monkeypatch):
    w = progress_world
    unit = w.unit(1, 10000)
    task = w.task((unit, 1, 10000))
    w.conn.commit()
    set_local_clock(monkeypatch, 'Asia/Seoul', w.day, 12, 0)
    headers = {'X-Anonymous-Id': w.user}
    first = api_client.post('/api/records', headers=headers, json={'taskId': task, 'result': 'partial', 'toPage': 6999})
    assert first.status_code == 200, first.text
    assert first.json()['data']['event']['reason'] == 'record'
    view = get_today(api_client, w).json()['data']
    assert view['progress']['rate'] == 70 and view['event'] is None and view['quiz']['open'] is False
    assert not notices(w)
    w.conn.commit()
    fixed = api_client.patch(f'/api/tasks/{task}/record', headers=headers, json={'toPage': 7000})
    assert fixed.status_code == 200, fixed.text
    view = get_today(api_client, w).json()['data']
    assert view['event']['reason'] == 'dayClose' and view['event']['donePages'] == 7000
    assert view['quiz']['open'] is True and len(notices(w)) == 1


@pytest.mark.parametrize('first_method', ['GET', 'POST'])
def test_simultaneous_get_and_post_share_one_daily_notice(api_client, progress_world, monkeypatch, first_method):
    from app.db.session import engine
    w = progress_world
    unit = w.unit(1, 100)
    w.task((unit, 1, 70), done=[(unit, 1, 70)], status='completed')
    last = w.task((unit, 71, 100))
    w.conn.commit()
    set_local_clock(monkeypatch, 'Asia/Seoul', w.day, 23, 30)
    arrived, release, mutex = Event(), Event(), Lock()
    seen = []
    key = str(uuid4())

    def short_wait(dbapi, _record, _proxy):
        if w.conn.dialect.name == 'sqlite':
            dbapi.execute('PRAGMA busy_timeout=50')

    def hold_notice(_conn, _cursor, _statement, _parameters, context, _many):
        if not context.isinsert or getattr(context.compiled.statement.table, 'name', None) != 'user_notices':
            return
        with mutex:
            if seen:
                return
            seen.append(True)
        arrived.set()
        assert release.wait(8)

    def send(method):
        if method == 'GET':
            return get_today(api_client, w)
        return api_client.post('/api/records', headers={'X-Anonymous-Id': w.user, 'Idempotency-Key': key},
            json={'taskId': last, 'result': 'done'})

    other_method = 'POST' if first_method == 'GET' else 'GET'
    event.listen(engine, 'checkout', short_wait)
    event.listen(engine, 'after_cursor_execute', hold_notice)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(send, first_method)
            try:
                assert arrived.wait(8)
                second = pool.submit(send, other_method).result(timeout=5)
                assert second.status_code == 409, second.text
            finally:
                release.set()
            emitted = first.result(timeout=5)
            assert emitted.status_code == 200, emitted.text
            assert emitted.json()['data']['event']['reason'] == 'dayClose'
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', hold_notice)
        event.remove(engine, 'checkout', short_wait)
    retry = send(other_method)
    assert retry.status_code == 200, retry.text
    event_value = retry.json()['data']['event']
    assert event_value is None or event_value['reason'] != 'dayClose'
    assert len(notices(w)) == 1
