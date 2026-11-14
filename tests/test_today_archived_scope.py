from uuid import uuid4
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_draft import TODAY, plan_case
from tests.test_plan_today_e2e import today_case, copy_task, preview


def test_archived_subject_work_is_not_a_today_rest_candidate(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    archived=copy_task(conn,case,day=TODAY)
    row=conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==archived)).mappings().one()
    material=dict(conn.execute(select(t.materials).where(t.materials.c.id==case['material'])).mappings().one())
    unit=dict(conn.execute(select(t.units).where(t.units.c.id==case['unit'])).mappings().one())
    mid,uid=str(uuid4()),str(uuid4())
    conn.execute(t.materials.insert().values(**{**material,'id':mid,'subject_id':case['active']}))
    conn.execute(t.units.insert().values(**{**unit,'id':uid,'material_id':mid}))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==archived).values(subject_id=case['active']))
    conn.execute(update(t.study_blocks).where(t.study_blocks.c.id==row['block_id']).values(subject_id=case['active'],unit_id=uid))
    conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id==archived).values(unit_id=uid))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['task']['id']).values(status='completed'))
    before=conn.execute(select(t.study_tasks)).all();conn.commit()
    response=preview(api_client,case,'rest')
    assert response.status_code==400 and response.json()['error']['code']=='NO_TASK',response.text
    assert conn.execute(select(t.study_tasks)).all()==before
