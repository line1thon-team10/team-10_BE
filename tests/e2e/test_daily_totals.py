from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.services.daily_totals import DailyTotalsService
from tests.test_progress_contract import progress_world


def service(world):
    user=dict(world.conn.execute(select(t.users).where(t.users.c.id==world.user)).mappings().one())
    return DailyTotalsService(world.conn,user)


def test_api_day_close_freezes_denominator_and_correction_only_changes_done(api_client,progress_world,end_focus):
    w=progress_world
    unit=w.unit(1,10)
    task=w.task((unit,1,10),done=[(unit,1,7)],status='partial')
    w.conn.commit()
    end_focus(w.user)
    headers={'X-Anonymous-Id':w.user,'Idempotency-Key':str(uuid4())}
    closed=api_client.post('/api/demo/next-day',headers=headers)
    assert closed.status_code==200,closed.text
    snapshot=dict(w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==w.user,
        t.daily_plan_totals.c.subject_id==w.subject['id'],t.daily_plan_totals.c.study_date==w.day)).mappings().one())
    assert snapshot['planned_total_pages']==10 and snapshot['finalized_at'] is not None
    assert service(w).view(w.day)['done']==7
    w.conn.commit()
    assert api_client.post('/api/demo/next-day',headers=headers).json()==closed.json()
    changed=api_client.patch(f'/api/tasks/{task}/record',headers={'X-Anonymous-Id':w.user},json={'toPage':9})
    assert changed.status_code==200,changed.text
    assert service(w).view(w.day)['done']==9
    assert service(w).view(w.day)['plan']==10
    current=dict(w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==w.user,
        t.daily_plan_totals.c.subject_id==w.subject['id'],t.daily_plan_totals.c.study_date==w.day)).mappings().one())
    assert current==snapshot


def test_current_plan_recomputes_but_finalized_plan_never_rewrites(progress_world):
    w=progress_world
    unit=w.unit(1,20)
    first=w.task((unit,1,10))
    w.conn.commit()
    svc=service(w)
    svc.snapshot(w.day,at=w.now)
    assert svc.view(w.day,at=w.now)['plan']==10
    w.task((unit,11,20),kind='remedial')
    assert svc.view(w.day,at=w.now)['plan']==20
    totals=svc.snapshot(w.day,finalize=True,at=w.now)
    assert totals[w.subject['id']]['planned_new_pages']==10
    assert totals[w.subject['id']]['planned_total_pages']==20
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==first).values(status='carried_over'))
    assert svc.snapshot(w.day,finalize=True,at=w.now+timedelta(hours=1))==totals
    assert svc.view(w.day,at=w.now)['plan']==20


def test_same_pdf_duplicates_within_task_are_unioned_but_supplement_counts_again(progress_world):
    w=progress_world
    unit=w.unit(1,10)
    w.task((unit,1,7),(unit,5,10),done=[(unit,1,10)],status='completed')
    w.task((unit,1,5),kind='remedial',done=[(unit,1,5)],status='completed')
    w.conn.commit()
    totals=service(w).snapshot(w.day,finalize=True,at=w.now)
    assert totals[w.subject['id']]['planned_new_pages']==10
    assert totals[w.subject['id']]['planned_total_pages']==15
    assert service(w).view(w.day,at=w.now)['done']==15


def test_future_never_predicts_completion_and_cannot_finalize(progress_world):
    from app.core.responses import ApiError
    w=progress_world
    unit=w.unit(1,10)
    tomorrow=w.day+timedelta(days=1)
    w.task((unit,1,10),done=[(unit,1,10)],status='completed',day=tomorrow)
    w.conn.commit()
    svc=service(w)
    assert svc.view(tomorrow,at=w.now)['done']==0
    with pytest.raises(ApiError) as error:
        svc.snapshot(tomorrow,finalize=True,at=w.now)
    assert error.value.code=='INVALID_INPUT'


def test_finalization_is_owner_scoped(api_client,progress_world):
    w=progress_world
    w.conn.commit()
    response=api_client.post('/api/session')
    assert response.status_code==200,response.text
    other=response.json()['data']['anonymousId']
    before=w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==other)).all()
    w.conn.commit()
    service(w).snapshot(w.day,finalize=True,at=w.now)
    assert w.conn.execute(select(t.daily_plan_totals).where(t.daily_plan_totals.c.user_id==other)).all()==before
