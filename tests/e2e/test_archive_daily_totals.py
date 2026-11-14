from datetime import timedelta
from sqlalchemy import select

from app.db import schema as t
from app.services.daily_totals import DailyTotalsService
from tests.e2e.test_archive_calendar import archive, future_rows, world
from tests.test_progress_contract import progress_world


def test_archived_future_totals_disappear_without_losing_frozen_past(api_client,world):
    w=world;past,future,review=future_rows(w)
    user=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    prior=w.day-timedelta(days=1)
    DailyTotalsService(w.conn,user).snapshot(prior,finalize=True,at=w.now)
    DailyTotalsService(w.conn,user).snapshot(w.day+timedelta(days=1),at=w.now)
    archive(w)
    persisted=w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==w.user)).all()
    data=w.get(api_client,'/api/monthly?month=2026-11')
    days={row['date']:row for row in data['days']}
    assert (days[prior.isoformat()]['plan'],days[prior.isoformat()]['done'])==(10,4)
    future_day=days[(w.day+timedelta(days=1)).isoformat()]
    assert (future_day['plan'],future_day['done'],future_day['rate'],future_day['planned'])==(0,0,0,False)
    assert w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==w.user)).all()==persisted
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==future))=='planned'
