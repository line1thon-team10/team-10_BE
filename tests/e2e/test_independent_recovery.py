"""Independent HTTP recovery checks: facts, obligation lineage, and recapture."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_replan_options_e2e import recovery_case, options, apply


def add_task(conn, case, *, kind, end, day):
    source = case['tasks'][0]
    block, task = str(uuid4()), str(uuid4())
    conn.execute(t.study_blocks.insert().values(id=block, subject_id=case['subject'], unit_id=case['unit'], task_type=kind, created_at=NOW))
    conn.execute(t.study_tasks.insert().values(id=task, plan_id=source['plan_id'], subject_id=case['subject'], block_id=block,
        task_type=kind, title='Independent obligation', scheduled_date=day, estimated_minutes=end * 3, status='planned', created_at=NOW))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=task, unit_id=case['unit'], start_page=1, end_page=end, sort_order=1))
    return task, block


@pytest.mark.parametrize('key', ['A', 'B', 'C'])
def test_each_option_preserves_recorded_pages_and_remedial_book(api_client, domain_connection, recovery_case, key):
    c, conn = recovery_case, domain_connection
    conn.execute(update(t.subjects).where(t.subjects.c.id == c['subject']).values(minutes_per_page=3))
    past, _ = add_task(conn, c, kind='study', end=5, day=TODAY-timedelta(days=1))
    rem, block = add_task(conn, c, kind='remedial', end=10, day=TODAY)
    concepts = {}
    for index, (start, end) in enumerate(((1, 5), (6, 10), (11, 20)), 1):
        identifier = str(uuid4())
        concepts[start] = identifier
        conn.execute(t.concepts.insert().values(id=identifier, unit_id=c['unit'], code=identifier, title='Whole concept',
            sort_order=index, start_page=start, end_page=end, in_scope=True, created_at=NOW, updated_at=NOW))
    conn.commit()
    for task, body in ((past, {'result': 'done'}), (rem, {'result': 'partial', 'toPage': 5})):
        response = api_client.post('/api/records', headers=c['headers'], json={'taskId': task, **body})
        assert response.status_code == 200, response.text
    records = [dict(r) for r in conn.execute(select(t.study_records).order_by(t.study_records.c.id)).mappings()]
    ranges = [dict(r) for r in conn.execute(select(t.study_record_ranges).order_by(t.study_record_ranges.c.id)).mappings()]
    past_row = dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == past)).mappings().one())
    conn.commit()
    response = options(api_client, c)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert next(o for o in value['options'] if o['key'] == key)['enabled'], response.text
    response = apply(api_client, c, value['previewId'], key)
    assert response.status_code == 200, response.text
    assert [dict(r) for r in conn.execute(select(t.study_records).order_by(t.study_records.c.id)).mappings()] == records
    assert [dict(r) for r in conn.execute(select(t.study_record_ranges).order_by(t.study_record_ranges.c.id)).mappings()] == ranges
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == past)).mappings().one()) == past_row
    rem_children = list(conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id == rem)).mappings())
    assert len(rem_children) == 1
    child = rem_children[0]
    assert (child['block_id'], child['task_type'], child['estimated_minutes']) == (block, 'remedial', 15)
    child_ranges = list(conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == child['id'])).mappings())
    assert [(r['start_page'], r['end_page']) for r in child_ranges] == [(6, 10)]
    active_study = select(t.study_tasks.c.id).where(t.study_tasks.c.plan_id == c['tasks'][0]['plan_id'],
        t.study_tasks.c.task_type == 'study', t.study_tasks.c.status == 'planned')
    new_pages = [p for r in conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(active_study))).mappings()
                 for p in range(r['start_page'], r['end_page']+1)]
    assert sorted(new_pages) == (list(range(6, 11)) if key == 'B' else list(range(6, 21)))
    assert conn.scalar(select(t.concepts.c.in_scope).where(t.concepts.c.id == concepts[1])) is True
    assert conn.scalar(select(t.concepts.c.in_scope).where(t.concepts.c.id == concepts[11])) is (key != 'B')
    planned_study = list(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id.in_(active_study))).mappings())
    assert sum(row['estimated_minutes'] for row in planned_study) == {'A':45, 'B':15, 'C':32}[key]


