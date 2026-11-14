from datetime import date, timedelta
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from app.services.daily_totals import DailyTotalsService
from tests.test_onboarding_events import events_user
from tests.test_progress_contract import progress_world


def get(client,user,month):
    return client.get('/api/monthly',headers={'X-Anonymous-Id':user},params={'month':month})


@pytest.mark.parametrize('month,size,start,end', [('2021-02',28,'2021-02-01','2021-02-28'),
    ('2024-02',35,'2024-01-29','2024-03-03'),('2026-11',42,'2026-10-26','2026-12-06'),
    ('2026-12',35,'2026-11-30','2027-01-03')])
def test_real_calendar_grid(api_client,events_user,month,size,start,end):
    response=get(api_client,events_user['id'],month)
    assert response.status_code==200,response.text
    rows=response.json()['data']['days']
    assert len(rows)==size and rows[0]['date']==start and rows[-1]['date']==end
    assert all(row['plan']==row['done']==row['rate']==0 and not row['estimated'] for row in rows)
    assert sum(row['inMonth'] for row in rows)==(28 if month=='2021-02' else 29 if month=='2024-02' else 30 if month=='2026-11' else 31)


@pytest.mark.parametrize('month',['2026-00','2026-13','0000-01','2026-1','2026-11-01','9999-12',''])
def test_invalid_month_is_422(api_client,events_user,month):
    assert get(api_client,events_user['id'],month).status_code==422


def test_daily_achievement_includes_supplement_but_new_pages_do_not(api_client,progress_world):
    w=progress_world;a=w.unit(1,10);b=w.unit(1,10)
    w.task((a,1,10),done=[(a,1,7)],status='partial')
    w.task((a,1,5),done=[(a,1,5)],status='completed',kind='remedial')
    w.task((b,1,10),done=[(b,1,3)],status='partial')
    w.task((a,1,10),done=[(a,1,10)],status='completed',day=w.day+timedelta(days=1))
    w.conn.commit()
    response=get(api_client,w.user,'2026-11')
    assert response.status_code==200,response.text
    days={d['date']:d for d in response.json()['data']['days']}
    day=days[w.day.isoformat()]
    assert (day['plan'],day['done'],day['rate'])==(25,15,60)
    assert next(row['pages'] for row in day['bySubject'] if row['subjectId']==w.subject['id'])==10
    # Historical seed records remain facts even when their old placement is excluded.
    tomorrow=days[(w.day+timedelta(days=1)).isoformat()]
    assert tomorrow['future'] and tomorrow['done']==0 and tomorrow['bySubject']==[]


def test_frozen_past_denominator_survives_carry_and_record_correction(api_client,progress_world):
    w=progress_world;unit=w.unit(1,10)
    task=w.task((unit,1,10),done=[(unit,1,7)],status='partial')
    user=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    DailyTotalsService(w.conn,user).snapshot(w.day,finalize=True,at=w.now)
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(status='carried_over'))
    record=w.conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id==task))
    w.conn.execute(update(t.study_record_ranges).where(t.study_record_ranges.c.record_id==record).values(end_page=9))
    w.conn.commit()
    rows=get(api_client,w.user,'2026-11').json()['data']['days']
    day=next(d for d in rows if d['date']==w.day.isoformat())
    assert (day['plan'],day['done'],day['rate'])==(10,9,90)
