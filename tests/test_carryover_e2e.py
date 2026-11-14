"""Real HTTP day closure carries actual disjoint ranges without losing facts."""
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.db import schema as t


@pytest.fixture
def carry_case(settled_demo_user, domain_connection):
    conn, user = domain_connection, settled_demo_user
    sid = conn.scalar(select(t.subjects.c.id).where(t.subjects.c.user_id == user, t.subjects.c.code == 'AI'))
    unit = conn.execute(select(t.units.c.id, t.materials.c.id.label('material_id')).join(t.materials)
                        .where(t.materials.c.subject_id == sid).order_by(t.units.c.sort_order)).mappings().first()
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.plan_id == select(t.plans.c.id).where(t.plans.c.user_id == user).scalar_subquery()).values(status='completed'))
    plan = conn.execute(select(t.plans).where(t.plans.c.user_id == user)).mappings().one()
    block, source = str(uuid4()), str(uuid4())
    at = datetime.now(timezone.utc)
    conn.execute(t.study_blocks.insert().values(id=block, subject_id=sid, unit_id=unit['id'], task_type='remedial', created_at=at))
    conn.execute(t.study_tasks.insert().values(id=source, plan_id=plan['id'], subject_id=sid, block_id=block,
        task_type='remedial', title='분리 범위 보충', scheduled_date=date(2026,11,9), estimated_minutes=40, status='planned', created_at=at))
    ids=[]
    for index, (start,end) in enumerate([(1,4),(7,10)],1):
        rid=str(uuid4()); ids.append(rid)
        conn.execute(t.study_task_ranges.insert().values(id=rid,task_id=source,unit_id=unit['id'],start_page=start,end_page=end,sort_order=index))
    conn.commit()
    return {'user':user,'subject':sid,'source':source,'block':block,'unit':unit['id'],'material':unit['material_id'],'ranges':ids,'revision':plan['revision']}


def test_carry_keeps_source_and_record_and_reports_exact_new_range_ids(api_client,domain_connection,carry_case):
    case,conn=carry_case,domain_connection
    headers={'X-Anonymous-Id':case['user']}
    result=api_client.post('/api/records',headers=headers,json={'taskId':case['source'],'result':'partial',
        'completedRanges':[{'rangeId':case['ranges'][0],'toPage':2},{'rangeId':case['ranges'][1],'toPage':8}]})
    assert result.status_code==200,result.text
    record=dict(conn.execute(select(t.study_records).where(t.study_records.c.task_id==case['source'])).mappings().one())
    ranges=[dict(r) for r in conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==case['source'])).mappings()]
    conn.commit()
    key=str(uuid4())
    first=api_client.post('/api/demo/next-day',headers={**headers,'Idempotency-Key':key})
    assert first.status_code==200,first.text
    child=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==case['source'])).mappings().one()
    assert child['block_id']==case['block'] and child['estimated_minutes']==20
    assert child['scheduled_date'] < date(2026,11,12)
    child_ranges=list(conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==child['id']).order_by(t.study_task_ranges.c.sort_order)).mappings())
    assert [(r['start_page'],r['end_page']) for r in child_ranges]==[(3,4),(9,10)]
    assert dict(conn.execute(select(t.study_records).where(t.study_records.c.id==record['id'])).mappings().one())==record
    assert [dict(r) for r in conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==case['source'])).mappings()]==ranges
    notice=conn.scalar(select(t.user_notices.c.payload).where(t.user_notices.c.user_id==case['user'],t.user_notices.c.notice_type=='carry'))
    item=next(item for item in notice['items'] if item['originTaskId']==case['source'])
    assert item['taskId']==child['id'] and item['pages']==4 and item['from'] is None and item['to'] is None
    assert {r['rangeId'] for r in item['ranges']}=={r['id'] for r in child_ranges}
    assert all(r['materialId']==case['material'] and r['unitId']==case['unit'] for r in item['ranges'])
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==case['user']))==case['revision']+1
    conn.commit()
    assert api_client.post('/api/demo/next-day',headers={**headers,'Idempotency-Key':key}).json()==first.json()
    assert conn.scalar(select(func.count()).select_from(t.study_tasks).where(t.study_tasks.c.origin_task_id==case['source']))==1


def test_no_next_subject_study_day_retains_obligation_without_inventing_date(api_client,domain_connection,carry_case):
    case,conn=carry_case,domain_connection
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.subject_id==case['subject'],t.study_tasks.c.scheduled_date>date(2026,11,9)).values(status='excluded'))
    conn.commit()
    response=api_client.post('/api/demo/next-day',headers={'X-Anonymous-Id':case['user']})
    assert response.status_code==200,response.text
    assert conn.scalar(select(func.count()).select_from(t.study_tasks).where(t.study_tasks.c.origin_task_id==case['source']))==0
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==case['source']))=='missed'
    assert conn.scalar(select(func.count()).select_from(t.study_task_ranges).where(t.study_task_ranges.c.task_id==case['source']))==2


@pytest.mark.parametrize('reason',['too_short','full_calendar'])
def test_impossible_carry_keeps_source_obligation_without_fake_duration(api_client,domain_connection,carry_case,reason):
    case,conn=carry_case,domain_connection
    if reason=='too_short':
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['source']).values(estimated_minutes=10))
    else:
        day=conn.scalar(select(func.min(t.study_tasks.c.scheduled_date)).where(t.study_tasks.c.subject_id==case['subject'],
            t.study_tasks.c.scheduled_date>date(2026,11,9),t.study_tasks.c.status.not_in(['carried_over','excluded'])))
        from zoneinfo import ZoneInfo
        start=datetime(day.year,day.month,day.day,8,tzinfo=ZoneInfo('Asia/Seoul'))
        end=start.replace(hour=23)
        at=datetime.now(timezone.utc)
        conn.execute(t.calendar_events.insert().values(id=str(uuid4()),user_id=case['user'],title='하루 일정',event_type='other',
            starts_at=start.astimezone(timezone.utc),ends_at=end.astimezone(timezone.utc),revision=1,created_at=at,updated_at=at))
    conn.commit()
    response=api_client.post('/api/demo/next-day',headers={'X-Anonymous-Id':case['user']})
    assert response.status_code==200,response.text
    assert conn.scalar(select(func.count()).select_from(t.study_tasks).where(t.study_tasks.c.origin_task_id==case['source']))==0
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==case['source']))=='missed'
