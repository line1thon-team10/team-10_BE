"""Independent monthly API checks distinguish placement and distinct page facts."""
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world


def day(client, user, date):
    response = client.get('/api/monthly', headers={'X-Anonymous-Id': user}, params={'month': date.strftime('%Y-%m')})
    assert response.status_code == 200, response.text
    return next(d for d in response.json()['data']['days'] if d['date'] == date.isoformat())


def test_new_pages_union_cross_task_pdf_ranges_but_total_counts_supplements(api_client, progress_world):
    w = progress_world
    first = w.unit(1, 10)
    overlap = w.unit(5, 15, material_id=first['material_id'])
    second = w.unit(1, 10)
    w.task((first, 1, 10), done=[(first, 1, 10)], status='completed')
    w.task((overlap, 5, 15), done=[(overlap, 5, 15)], status='completed')
    w.task((second, 1, 10), done=[(second, 1, 10)], status='completed')
    w.task((first, 1, 5), done=[(first, 1, 5)], status='completed', kind='remedial')
    w.conn.commit()
    result = day(api_client, w.user, w.day)
    assert (result['plan'], result['done'], result['rate']) == (36, 36, 100)
    assert next(r['pages'] for r in result['bySubject'] if r['subjectId'] == w.subject['id']) == 25
    future = day(api_client, w.user, w.day + timedelta(days=1))
    assert future['done'] == 0 and future['bySubject'] == []


def test_real_close_then_correction_keeps_monthly_denominator_and_future_carry_unearned(api_client, progress_world, end_focus):
    w = progress_world
    unit = w.unit(1, 1000)
    task = w.task((unit, 1, 10), done=[(unit, 1, 4)], status='partial')
    w.conn.commit()
    end_focus(w.user)
    response = api_client.post('/api/demo/next-day', headers={'X-Anonymous-Id': w.user, 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 200, response.text
    before = w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id == w.user)).all()
    w.conn.commit()
    old = day(api_client, w.user, w.day)
    assert (old['plan'], old['done']) == (10, 4)
    assert day(api_client, w.user, w.day + timedelta(days=1))['done'] == 0
    response = api_client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': w.user}, json={'toPage': 9})
    assert response.status_code == 200, response.text
    updated = day(api_client, w.user, w.day)
    assert (updated['plan'], updated['done'], updated['rate']) == (10, 9, 90)
    assert next(r['pages'] for r in updated['bySubject'] if r['subjectId'] == w.subject['id']) == 9
    assert w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id == w.user)).all() == before


def test_archived_subject_and_retired_unit_keep_historical_facts_without_read_writes(api_client, progress_world):
    w = progress_world
    unit = w.unit(1, 10)
    w.task((unit, 1, 10), done=[(unit, 1, 6)], status='partial')
    w.conn.execute(update(t.subjects).where(t.subjects.c.id == w.subject['id']).values(status='archived', archive_reason='user_removed'))
    w.conn.execute(update(t.units).where(t.units.c.id == unit['id']).values(retired_at=w.now))
    before = {table.name: w.conn.execute(select(table)).all() for table in (t.daily_plan_totals, t.study_records, t.study_tasks)}
    w.conn.commit()
    own = day(api_client, w.user, w.day)
    assert (own['plan'], own['done']) == (10, 6)
    assert next(r['pages'] for r in own['bySubject'] if r['subjectId'] == w.subject['id']) == 6
    after = {table.name: w.conn.execute(select(table)).all() for table in (t.daily_plan_totals, t.study_records, t.study_tasks)}
    assert before == after
    w.conn.commit()
    other = api_client.post('/api/session').json()['data']['anonymousId']
    foreign = day(api_client, other, w.day)
    assert all(row['subjectId'] != w.subject['id'] for row in foreign['bySubject'])
