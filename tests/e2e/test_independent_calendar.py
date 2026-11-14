"""Independent calendar projection checks using persisted schedule exceptions."""
from datetime import timedelta
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_onboarding_events import events_user, add_event, add_exception, MONDAY, at


def query(client, user, first, last):
    response = client.get('/api/calendar', headers={'X-Anonymous-Id': user['id']}, params={'from': first, 'to': last})
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_move_out_and_move_in_keep_original_identity_across_month(api_client, domain_connection, events_user):
    conn, user = domain_connection, events_user
    event = add_event(conn, user, recurrence_rule='FREQ=WEEKLY;BYDAY=MO', recurrence_until=MONDAY + timedelta(days=7))
    add_exception(conn, event, MONDAY, kind='moved', moved_day=MONDAY + timedelta(days=28))
    incoming = MONDAY + timedelta(days=7)
    add_exception(conn, event, incoming, kind='moved', moved_day=MONDAY + timedelta(days=1))
    conn.commit()
    week = query(api_client, user, '2026-11-09', '2026-11-15')['fixed']
    assert [(r['id'], r['date']) for r in week] == [(event['id'] + '@2026-11-16', '2026-11-10')]
    december = query(api_client, user, '2026-12-01', '2026-12-07')['fixed']
    assert [(r['id'], r['date']) for r in december] == [(event['id'] + '@2026-11-09', '2026-12-07')]


def test_midnight_end_has_no_zero_length_duplicate_and_reads_do_not_write(api_client, domain_connection, events_user):
    conn, user = domain_connection, events_user
    event = add_event(conn, user, day=MONDAY, start='23:00', end='23:59')
    conn.execute(update(t.calendar_events).where(t.calendar_events.c.id == event['id']).values(ends_at=at(MONDAY + timedelta(days=1), '00:00')))
    before = conn.execute(select(t.calendar_events)).all()
    conn.commit()
    data = query(api_client, user, '2026-11-09', '2026-11-10')
    assert len(data['fixed']) == 1
    assert (data['fixed'][0]['start'], data['fixed'][0]['end']) == ('23:00', '24:00')
    assert query(api_client, user, '2026-11-10', '2026-11-10')['fixed'] == []
    assert conn.execute(select(t.calendar_events)).all() == before


def test_recurring_wall_clock_is_preserved_across_dst(api_client, domain_connection, events_user):
    conn, user = domain_connection, events_user
    user = {**user, 'timezone': 'America/New_York'}
    conn.execute(update(t.users).where(t.users.c.id == user['id']).values(timezone=user['timezone']))
    day = MONDAY - timedelta(days=15)  # Sunday before the November clock change
    event = add_event(conn, user, day=day, start='09:00', end='10:00', recurrence_rule='FREQ=WEEKLY;BYDAY=SU')
    conn.commit()
    rows = query(api_client, user, '2026-10-25', '2026-11-01')['fixed']
    assert [r['date'] for r in rows] == ['2026-10-25', '2026-11-01']
    assert all((r['start'], r['end']) == ('09:00', '10:00') for r in rows)
    assert len({r['id'] for r in rows}) == 2
