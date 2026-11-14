"""Preserve exact placement duration and heterogeneous partial range weights."""
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_draft import TODAY, NOW, plan_case
from tests.test_plan_today_e2e import today_case, copy_task, preview, apply


def test_more_keeps_fractional_minutes_instead_of_display_rounding(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    task=copy_task(conn,case,minutes=45)
    row=conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==task)).mappings().one()
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(ends_at=row['ends_at']+timedelta(seconds=30),estimated_minutes=46));conn.commit()
    response=preview(api_client,case)
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId']).status_code==200
    moved=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==task)).mappings().one()
    assert (moved['ends_at']-moved['starts_at']).total_seconds()==2730
    assert moved['estimated_minutes']==46


def test_partial_composite_uses_stored_part_weights_not_page_ratio(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    task=case['task']['id'];copy_task(conn,case,minutes=30)
    second,record=str(uuid4()),str(uuid4())
    old=dict(conn.execute(select(t.units).where(t.units.c.id==case['unit'])).mappings().one())
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(end_page=10))
    conn.execute(t.units.insert().values(**{**old,'id':second,'code':'unit2','start_page':11,'sort_order':2}))
    conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id==task).values(end_page=10,planned_minutes=20))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task,unit_id=second,start_page=11,end_page=20,planned_minutes=30,sort_order=2))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(status='partial'))
    conn.execute(t.study_records.insert().values(id=record,task_id=task,result='partial',study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=10));conn.commit()
    response=preview(api_client,case,'postpone',taskId=task)
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId'],'postpone').status_code==200
    moved=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==task)).mappings().one()
    assert moved['estimated_minutes']==30
    assert (moved['ends_at']-moved['starts_at']).total_seconds()==1800
    ranges=conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==moved['id'])).mappings().all()
    assert [(row['unit_id'],row['start_page'],row['end_page']) for row in ranges]==[(second,11,20)]
