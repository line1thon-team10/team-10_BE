"""A real-day candidate must not silently apply a placement whose time has elapsed."""
from datetime import timedelta
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_draft import NOW, plan_case
from tests.test_plan_today_e2e import today_case, copy_task, preview, apply


def test_elapsed_real_day_candidate_cannot_apply_past_placement(api_client,domain_connection,today_case,monkeypatch):
    from app.services import proposals, schedule_placements
    conn,case=domain_connection,today_case
    future=copy_task(conn,case,minutes=45)
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=None));conn.commit()
    clock=[NOW+timedelta(hours=1)]  # 10:00 local time, then 11:00.
    original=proposals.ProposalService.__init__
    def frozen_init(self,connection,**kwargs):
        return original(self,connection,clock=lambda:clock[0])
    monkeypatch.setattr(proposals.ProposalService,'__init__',frozen_init)
    monkeypatch.setattr(schedule_placements,'now_utc',lambda:clock[0])
    response=preview(api_client,case)
    assert response.status_code==200,response.text
    clock[0]+=timedelta(hours=1)
    response=apply(api_client,case,response.json()['data']['previewId'])
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==future))=='planned'
    assert conn.scalar(select(t.plans.c.revision))==1
