"""Independent manage.md range-addition checks through HTTP and persisted facts."""
from collections import Counter
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


@pytest.fixture
def case(domain_connection, addition_case):
    conn = domain_connection
    conn.execute(update(t.materials).where(t.materials.c.id == addition_case['material']).values(page_count=50))
    conn.commit()
    return addition_case


def request(case, **overrides):
    return {'subjectId': case['subject'], 'materialId': case['material'], 'from': 21, 'to': 26,
            'totalPages': 6, 'days': [2, 3], 'startDate': '2026-11-17', 'endDate': '2026-11-18', **overrides}


def preview(client, case, **overrides):
    return client.post('/api/plan/additions/preview', headers={'X-Anonymous-Id': case['user']}, json=request(case, **overrides))


def approve(client, case, identifier, **extra):
    return client.post('/api/plan/additions', headers={'X-Anonymous-Id': case['user']}, json={'previewId': identifier, **extra})


def snapshot(conn):
    return {table.name: conn.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in (t.subjects, t.materials, t.units, t.concepts, t.plans, t.study_blocks,
                          t.study_tasks, t.study_task_ranges, t.study_sessions, t.study_records, t.study_record_ranges)}


def placement_pages(value):
    return Counter((part['materialId'], page) for row in value['placements'] for part in row['ranges']
                   for page in range(part['from'], part['to'] + 1))


def pending_pages(conn, case):
    rows = conn.execute(select(t.units.c.material_id, t.study_task_ranges.c.start_page, t.study_task_ranges.c.end_page)
        .join(t.study_task_ranges, t.study_task_ranges.c.unit_id == t.units.c.id)
        .join(t.study_tasks, t.study_tasks.c.id == t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.subject_id == case['subject'], t.study_tasks.c.status == 'planned',
               t.study_tasks.c.task_type == 'study')).all()
    return Counter((row.material_id, page) for row in rows for page in range(row.start_page, row.end_page + 1))


@pytest.mark.parametrize('field', ['subjectId', 'materialId'])
def test_orphan_and_foreign_ids_never_change_existing_plan(api_client, domain_connection, case, field):
    conn = domain_connection
    before = snapshot(conn); conn.commit()
    missing = preview(api_client, case, **{field: str(uuid4())})
    assert missing.status_code == 404, missing.text
    foreign = preview(api_client, {**case, 'user': case['other_user']})
    assert foreign.status_code in (400, 404), foreign.text
    assert snapshot(conn) == before



@pytest.mark.parametrize('override', [
    {'from': 0}, {'to': 51}, {'from': 27, 'to': 26}, {'days': [2, 2]}, {'days': [0]},
    {'days': []}, {'totalPages': 7}, {'totalPages': None}, {'minutesPerDay': 20},
    {'totalPages': None, 'minutesPerDay': True}, {'endDate': '2026-11-19'},
    {'startDate': '2026-11-15'}, {'startDate': '2026-11-18', 'endDate': '2026-11-17'},
])
def test_invalid_goals_ranges_dates_and_days_are_atomic(api_client, domain_connection, case, override):
    before = snapshot(domain_connection); domain_connection.commit()
    response = preview(api_client, case, **override)
    assert response.status_code == 422, response.text
    assert snapshot(domain_connection) == before



@pytest.mark.parametrize('unavailable', ['wrong_weekday', 'daily_cap', 'calendar_full'])
def test_unplaceable_selection_reports_remaining_and_cannot_apply(api_client, domain_connection, case, unavailable):
    conn = domain_connection
    changes = {}
    if unavailable == 'wrong_weekday':
        changes['days'] = [1]
    elif unavailable == 'daily_cap':
        changes.update(totalPages=None, minutesPerDay=1)
    else:
        conn.execute(t.calendar_events.insert().values(id=str(uuid4()), user_id=case['user'], event_type='other',
            title='연속 일정', starts_at=NOW + timedelta(days=1) - timedelta(hours=9),
            ends_at=NOW + timedelta(days=3) - timedelta(hours=9), created_at=NOW, updated_at=NOW))
        conn.commit()
    before = snapshot(conn); conn.commit()
    response = preview(api_client, case, **changes)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['conflicts'] > 0 and value['leftPages'] == 6 and value['note']
    assert value['placements'] == []
    result = approve(api_client, case, value['previewId'])
    assert result.status_code == 409 and result.json()['error']['code'] == 'PLAN_INFEASIBLE'
    assert snapshot(conn) == before



def test_time_goal_obeys_per_day_cap_and_requested_dates(api_client, domain_connection, case):
    response = preview(api_client, case, totalPages=None, minutesPerDay=9)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['conflicts'] == value['leftPages'] == 0
    assert placement_pages(value) == Counter((case['material'], page) for page in range(21, 27))
    daily = Counter()
    for placement in value['placements']:
        assert placement['date'] in ['2026-11-17', '2026-11-18']
        assert placement['start'] < placement['end'] and placement['reason']
        daily[placement['date']] += placement['minutes']
    assert daily and all(0 < minutes <= 9 for minutes in daily.values())
    assert approve(api_client, case, value['previewId']).status_code == 200



