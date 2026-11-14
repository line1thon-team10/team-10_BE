from tests.test_plan_draft import plan_case
"""The 140-page/eight-day boundary uses ceil(17.5 * .5), not rounded averages."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_replan_options_e2e import recovery_case
from tests.test_plan_draft import TODAY, NOW


@pytest.mark.parametrize('pages,judgment',[(8,'carry'),(9,'replan')])
def test_eight_pages_carry_nine_pages_replan(api_client,domain_connection,recovery_case,pages,judgment):
    case,conn=recovery_case,domain_connection
    # The existing recovery fixture is unrelated; a fresh closure must assess
    # the exact 140p/8 distinct original study-day denominator below.
    conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id==case['request']).values(status='stale'))
    conn.execute(update(t.materials).where(t.materials.c.id==case['material']).values(page_count=140))
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(end_page=140))
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['subject']).values(exam_date=TODAY+timedelta(days=8)))
    source=case['tasks'][0]
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==source['id']).values(scheduled_date=TODAY-timedelta(days=1),estimated_minutes=pages*3))
    conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id==source['id']).values(start_page=1,end_page=pages))
    for offset in range(7):
        block,task=str(uuid4()),str(uuid4())
        conn.execute(t.study_blocks.insert().values(id=block,subject_id=case['subject'],unit_id=case['unit'],task_type='study',created_at=NOW))
        conn.execute(t.study_tasks.insert().values(id=task,plan_id=source['plan_id'],subject_id=case['subject'],block_id=block,
            task_type='study',title='original study day',scheduled_date=TODAY+timedelta(days=offset),estimated_minutes=15,
            status='planned',created_at=NOW))
        conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task,unit_id=case['unit'],start_page=21+offset*10,end_page=25+offset*10,sort_order=1))
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=TODAY-timedelta(days=1)))
    conn.commit()
    response=api_client.post('/api/demo/next-day',headers=case['headers'])
    assert response.status_code==200,response.text
    assert response.json()['data']['judgment']==judgment
    if judgment=='replan':
        ctx=conn.scalar(select(t.plan_change_requests.c.recovery_context).where(t.plan_change_requests.c.user_id==case['user'],
            t.plan_change_requests.c.kind=='recovery',t.plan_change_requests.c.status=='ready'))
        assert ctx['subjects']==[{'subjectId':case['subject'],'overduePages':9,'dailyAveragePages':17.5,'thresholdPages':9}]
    else:
        assert conn.scalar(select(t.study_tasks.c.id).where(t.study_tasks.c.origin_task_id==source['id'])) is not None
