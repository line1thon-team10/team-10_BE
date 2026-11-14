"""Independent archive visibility across actual day-close transactions."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.e2e.test_monthly_report import world
from tests.e2e.test_archive_calendar import archive, future_rows


@pytest.mark.parametrize('applied', [True, False])
def test_actual_day_closes_never_resurrect_archived_future_obligations(api_client, world, end_focus, applied):
    w = world
    past, future, review = future_rows(w)
    archive(w, applied=applied, **({} if applied else {'archived_at': None}))
    w.conn.commit()
    end_focus(w.user)
    def target_day():
        data = w.get(api_client, '/api/monthly?month=2026-11')
        return next(d for d in data['days'] if d['date'] == (w.day + timedelta(days=1)).isoformat())
    before = target_day()
    assert before['plan'] == 0 and not before['planned']
    for _ in range(2):
        response = api_client.post('/api/demo/next-day', headers={
            'X-Anonymous-Id': w.user, 'Idempotency-Key': str(uuid4())})
        assert response.status_code == 200, response.text
    after = target_day()
    assert (after['plan'], after['done'], after['rate'], after['planned']) == (0, 0, 0, False)
    assert after['labels'] == []
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == future)) == 'planned'


def test_finalizing_hidden_archived_date_keeps_zero_denominator(api_client, world):
    from app.services.daily_totals import DailyTotalsService
    w = world
    future_rows(w)
    archive(w)
    hidden_day = w.day + timedelta(days=1)
    w.conn.execute(update(t.users).where(t.users.c.id == w.user).values(demo_date=hidden_day))
    owner = dict(w.conn.execute(select(t.users).where(t.users.c.id == w.user)).mappings().one())
    svc = DailyTotalsService(w.conn, owner)
    assert svc.view(hidden_day)['plan'] == 0
    svc.snapshot(hidden_day, finalize=True, at=w.now)
    assert svc.view(hidden_day)['plan'] == 0
