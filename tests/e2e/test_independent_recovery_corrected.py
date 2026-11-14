"""P2 must retain pages reopened by correction behind a protected carryover."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_replan_options_e2e import recovery_case, options, apply
from tests.e2e.test_independent_recovery import add_task


@pytest.mark.parametrize('state', ['running', 'paused', 'ended'])
def test_corrected_ancestor_waits_for_protected_child_then_reopens_same_book(api_client, domain_connection, recovery_case, monkeypatch, state):
    from app.services import focus
    c, conn = recovery_case, domain_connection
    clock = [NOW]
    monkeypatch.setattr(focus, 'now', lambda: clock[0])
    root, block = add_task(conn, c, kind='remedial', end=20, day=TODAY-timedelta(days=1))
    conn.commit()
    recorded = api_client.post('/api/records', headers=c['headers'], json={'taskId':root, 'result':'partial', 'toPage':8})
    assert recorded.status_code == 200, recorded.text
    original = dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == root)).mappings().one())
    child = str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == root).values(status='carried_over'))
    conn.execute(t.study_tasks.insert().values(**{**original, 'id':child, 'origin_task_id':root,
        'scheduled_date':TODAY, 'status':'planned', 'estimated_minutes':30}))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=child, unit_id=c['unit'], start_page=9, end_page=20, sort_order=1))
    conn.commit()
    started = api_client.post('/api/study/sessions', headers=c['headers'], json={'taskId':child})
    assert started.status_code == 200, started.text
    session = started.json()['data']['id']
    clock[0] += timedelta(minutes=10)
    if state != 'running':
        operation = 'pause' if state == 'paused' else 'end'
        response = api_client.post(f'/api/study/sessions/{session}/{operation}', headers=c['headers'])
        assert response.status_code == 200, response.text
    corrected = api_client.patch(f'/api/tasks/{root}/record', headers=c['headers'], json={'toPage':2})
    assert corrected.status_code == 200, corrected.text
    blocked = options(api_client, c)
    assert blocked.status_code == 200, blocked.text
    assert all(not item['enabled'] for item in blocked.json()['data']['options']), blocked.text
    if state != 'ended':
        ended = api_client.post(f'/api/study/sessions/{session}/end', headers=c['headers'])
        assert ended.status_code == 200, ended.text
    completed = api_client.post('/api/records', headers=c['headers'], json={'taskId':child, 'result':'done', 'sessionId':session})
    assert completed.status_code == 200, completed.text
    fresh = options(api_client, c)
    assert fresh.status_code == 200, fresh.text
    value = fresh.json()['data']
    assert value['previewId'] != blocked.json()['data']['previewId']
    assert next(item for item in value['options'] if item['key']=='C')['enabled'], fresh.text
    approved = apply(api_client, c, value['previewId'], 'C')
    assert approved.status_code == 200, approved.text
    live = list(conn.execute(select(t.study_tasks).where(t.study_tasks.c.block_id == block, t.study_tasks.c.status == 'planned')).mappings())
    assert len(live) == 1 and live[0]['origin_task_id'] == root and live[0]['estimated_minutes'] == 15
    spans = list(conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == live[0]['id'])).mappings())
    assert [(row['start_page'],row['end_page']) for row in spans] == [(3,8)]
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == child)) == 'completed'
    records = list(conn.execute(select(t.study_records).where(t.study_records.c.task_id.in_([root,child]))).mappings())
    assert len(records) == 2
