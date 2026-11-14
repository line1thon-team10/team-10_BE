"""Independent recovery HTTP edge checks."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.scheduling import SearchLimit
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_replan_options_e2e import recovery_case, options, apply
from tests.test_replan_safety_e2e import snapshot


def test_real_day_close_today_and_saved_options_share_one_approvable_candidate(api_client, domain_connection, recovery_case):
    c, conn = recovery_case, domain_connection
    conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == c['request']).values(status='stale'))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.plan_id == c['tasks'][0]['plan_id']).values(scheduled_date=TODAY, starts_at=None, ends_at=None))
    conn.commit()
    closed = api_client.post('/api/demo/next-day', headers=c['headers'])
    assert closed.status_code == 200 and closed.json()['data']['judgment'] == 'replan', closed.text
    today = api_client.get('/api/today', headers=c['headers'])
    assert today.status_code == 200, today.text
    prompted = today.json()['data']['replan']
    regular = options(api_client, c)
    saved = api_client.get('/api/plan/replan/options?saved=1', headers=c['headers'])
    assert regular.status_code == saved.status_code == 200
    assert prompted['previewId'] == regular.json()['data']['previewId'] == saved.json()['data']['previewId']
    assert saved.json()['data']['saved'] is True and regular.json()['data']['saved'] is False
    assert prompted['totalPages'] == 20
    assert apply(api_client, c, prompted['previewId'], 'C').status_code == 200
    after = api_client.get('/api/today', headers=c['headers'])
    assert after.status_code == 200 and after.json()['data']['replan'] is None, after.text
    assert options(api_client, c).status_code == 404
    live = list(conn.execute(select(t.study_tasks).where(t.study_tasks.c.plan_id == c['tasks'][0]['plan_id'], t.study_tasks.c.status == 'planned')).mappings())
    assert sum(row['estimated_minutes'] for row in live) == 35
    spans = list(conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_([row['id'] for row in live]))).mappings())
    assert sorted(p for row in spans for p in range(row['start_page'], row['end_page']+1)) == list(range(1, 21))
    assert all(row['scheduled_date'] > TODAY for row in live)
