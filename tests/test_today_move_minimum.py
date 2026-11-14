"""Partial movements cannot invent minutes to satisfy the allocator minimum."""
from uuid import uuid4
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_today_e2e import today_case, copy_task, preview


def test_too_short_remainder_does_not_invent_fifteen_minutes(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    task=case['task']['id'];copy_task(conn,case)
    record=str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(status='partial'))
    conn.execute(t.study_records.insert().values(id=record,task_id=task,result='partial',study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=19))
    before=conn.execute(select(t.study_tasks)).all();conn.commit()
    response=preview(api_client,case,'postpone',taskId=task)
    assert response.status_code==409 and response.json()['error']['code']=='PLAN_INFEASIBLE',response.text
    assert conn.execute(select(t.study_tasks)).all()==before
