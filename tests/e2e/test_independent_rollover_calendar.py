"""Cross-domain checks: ordinary date settlement and archive calendar reads."""
from datetime import date, timedelta

from sqlalchemy import select, update

from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.e2e.test_independent_day_rollover import clock, ordinary


def test_calendar_auth_archives_and_preserves_closed_monthly_facts(api_client,progress_world,end_focus,clock):
    w=progress_world;unit=w.unit(1,100)
    past=w.task((unit,1,10),done=[(unit,1,4)],status='partial')
    future=w.task((unit,11,20),kind='remedial',day=date(2026,11,12))
    w.conn.execute(update(t.subjects).where(t.subjects.c.id==w.subject['id']).values(exam_date=date(2026,11,10)))
    before=w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).one()
    w.conn.commit();end_focus(w.user);ordinary(w)
    headers={'X-Anonymous-Id':w.user}
    response=api_client.get('/api/calendar?from=2026-11-09&to=2026-11-15',headers=headers)
    assert response.status_code==200,response.text
    assert any(row['id']==past for row in response.json()['data']['study'])
    assert not any(row['id']==future for row in response.json()['data']['study'])
    assert w.conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id==w.subject['id']))=='archived'
    frozen=dict(w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==w.user,
        t.daily_plan_totals.c.subject_id==w.subject['id'],t.daily_plan_totals.c.study_date==w.day)).mappings().one())
    assert frozen['planned_total_pages']==10 and frozen['finalized_at'] is not None
    w.conn.commit();clock[0]+=timedelta(days=3)
    response=api_client.get('/api/monthly?month=2026-11',headers=headers)
    assert response.status_code==200,response.text
    days={row['date']:row for row in response.json()['data']['days']}
    assert (days['2026-11-09']['plan'],days['2026-11-09']['done'])==(10,4)
    assert not days['2026-11-12']['planned'] and days['2026-11-12']['plan']==0
    assert w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).one()==before
    assert dict(w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==w.user,
        t.daily_plan_totals.c.subject_id==w.subject['id'],t.daily_plan_totals.c.study_date==w.day)).mappings().one())==frozen
