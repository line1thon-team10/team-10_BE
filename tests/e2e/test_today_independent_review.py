"""Independent U05 HTTP contract review; assertions include immutable DB facts."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update, delete
from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_today_e2e import today_case, copy_task, preview, apply


def rows(conn, table):
    return sorted((dict(r) for r in conn.execute(select(table)).mappings()), key=lambda r: r['id'])


def record(conn, case, task, parts):
    identifier = str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task).values(status='partial'))
    conn.execute(t.study_records.insert().values(id=identifier,task_id=task,result='partial',study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    for unit, start, end in parts:
        conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=identifier,unit_id=unit,start_page=start,end_page=end))


def second_pdf(conn, case):
    material = dict(conn.execute(select(t.materials).where(t.materials.c.id == case['material'])).mappings().one())
    unit = dict(conn.execute(select(t.units).where(t.units.c.id == case['unit'])).mappings().one())
    mid, uid = str(uuid4()), str(uuid4())
    conn.execute(t.materials.insert().values(**{**material,'id':mid,'sha256':mid,'page_count':10}))
    conn.execute(t.units.insert().values(**{**unit,'id':uid,'material_id':mid,'end_page':10}))
    return uid


@pytest.mark.parametrize('gap_minutes',[55,54])
def test_more_uses_whole_exact_duration_and_never_squeezes_into_short_gap(api_client,domain_connection,today_case,gap_minutes):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case,minutes=30)
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['task']['id']).values(ends_at=NOW))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==future).values(ends_at=NOW+timedelta(days=1,minutes=55,seconds=30)))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==future).values(starts_at=NOW+timedelta(days=1)))
    conn.execute(t.calendar_events.insert().values(id=str(uuid4()),user_id=case['user'],event_type='job',title='day full',starts_at=NOW+timedelta(minutes=gap_minutes,seconds=30),ends_at=NOW+timedelta(hours=14),created_at=NOW,updated_at=NOW))
    before=rows(conn,t.study_tasks);conn.commit()
    response=preview(api_client,case)
    if gap_minutes==54:
        assert response.status_code==409,response.text
        assert response.json()['error']['code']=='PLAN_INFEASIBLE'
        assert rows(conn,t.study_tasks)==before
    else:
        assert response.status_code==200,response.text
        assert apply(api_client,case,response.json()['data']['previewId']).status_code==200
        moved=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==future)).mappings().one()
        assert moved['ends_at']-moved['starts_at']==timedelta(minutes=55,seconds=30)
        assert moved['estimated_minutes']==56 and moved['scheduled_date']==TODAY


def test_postpone_multi_pdf_partial_uses_weighted_remainder_and_preserves_facts(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    task=case['task']['id'];other=second_pdf(conn,case)
    copy_task(conn,case,minutes=20)
    conn.execute(delete(t.study_task_ranges).where(t.study_task_ranges.c.task_id==task))
    for order,uid,minutes in [(1,case['unit'],20),(2,other,40)]:
        conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task,unit_id=uid,start_page=1,end_page=10,planned_minutes=minutes,sort_order=order))
    # This is a legacy heterogeneous snapshot, so use each range's persisted estimate.
    conn.execute(update(t.plan_proposals).values(changes={}))
    record(conn,case,task,[(case['unit'],1,10),(other,1,2)])
    facts=(rows(conn,t.study_records),rows(conn,t.study_record_ranges));conn.commit()
    response=preview(api_client,case,'postpone',taskId=task)
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId'],'postpone').status_code==200
    moved=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==task)).mappings().one()
    assert moved['ends_at']-moved['starts_at']==timedelta(minutes=32)
    parts=conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==moved['id'])).mappings().all()
    assert [(p['unit_id'],p['start_page'],p['end_page']) for p in parts]==[(other,3,10)]
    assert (rows(conn,t.study_records),rows(conn,t.study_record_ranges))==facts


def test_rest_moves_all_today_pdf_remainders_once_and_retains_completed_history(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    other=second_pdf(conn,case);task=case['task']['id']
    second=copy_task(conn,case,day=TODAY,hour=10,minutes=25,start=1,end=10)
    conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id==second).values(unit_id=other))
    record(conn,case,task,[(case['unit'],1,5)])
    record(conn,case,second,[(other,1,3)])
    facts=(rows(conn,t.study_records),rows(conn,t.study_record_ranges));conn.commit()
    response=preview(api_client,case,'rest')
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId'],'rest').status_code==200
    pending=conn.execute(select(t.study_tasks).where(t.study_tasks.c.status=='planned')).mappings().all()
    assert pending and all(TODAY<p['scheduled_date']<TODAY+timedelta(days=3) for p in pending)
    parts=conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_([p['id'] for p in pending]))).mappings().all()
    pages=[(p['unit_id'],n) for p in parts for n in range(p['start_page'],p['end_page']+1)]
    expected=[(case['unit'],n) for n in range(6,21)]+[(other,n) for n in range(4,11)]
    assert sorted(pages)==sorted(expected)
    assert (rows(conn,t.study_records),rows(conn,t.study_record_ranges))==facts


@pytest.mark.parametrize('fact',['running','paused','ended','record'])
def test_new_focus_or_record_fact_stales_preview_without_mutation(api_client,domain_connection,today_case,fact):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case);conn.commit()
    candidate=preview(api_client,case).json()['data']['previewId']
    if fact=='record': record(conn,case,future,[(case['unit'],1,5)])
    else:
        conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=future,status=fact,started_at=NOW,ended_at=NOW+timedelta(minutes=1) if fact=='ended' else None,study_date=TODAY,created_at=NOW))
    before=rows(conn,t.study_tasks);conn.commit()
    response=apply(api_client,case,candidate)
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert rows(conn,t.study_tasks)==before
    assert conn.scalar(select(t.plans.c.revision))==1


def test_postpone_skips_other_subject_day_and_precedes_fixed_next_day_task(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    future=copy_task(conn,case,day=TODAY+timedelta(days=2),hour=12,minutes=40)
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=future,status='ended',started_at=NOW,ended_at=NOW+timedelta(minutes=1),study_date=TODAY,created_at=NOW))
    original=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).mappings().one());conn.commit()
    response=preview(api_client,case,'postpone',taskId=case['task']['id'])
    assert response.status_code==200,response.text
    assert apply(api_client,case,response.json()['data']['previewId'],'postpone').status_code==200
    moved=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==case['task']['id'])).mappings().one()
    assert moved['scheduled_date']==TODAY+timedelta(days=2)
    assert moved['ends_at']<=original['starts_at']
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==future)).mappings().one())==original


def test_foreign_real_task_cannot_be_previewed_or_moved(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    foreign=copy_task(conn,case,day=TODAY,hour=12)
    foreign_unit=second_pdf(conn,case)
    foreign_material=conn.scalar(select(t.units.c.material_id).where(t.units.c.id==foreign_unit))
    conn.execute(update(t.materials).where(t.materials.c.id==foreign_material).values(subject_id=case['foreign']))
    conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id==foreign).values(unit_id=foreign_unit,end_page=10))
    block=conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id==foreign))
    conn.execute(update(t.study_blocks).where(t.study_blocks.c.id==block).values(subject_id=case['foreign'],unit_id=foreign_unit))
    plan=str(uuid4())
    conn.execute(t.plans.insert().values(id=plan,user_id=case['other_user'],revision=1,created_at=NOW,approved_at=NOW))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==foreign).values(plan_id=plan,subject_id=case['foreign']))
    before=rows(conn,t.study_tasks);conn.commit()
    response=preview(api_client,case,'postpone',taskId=foreign)
    assert response.status_code==404 and response.json()['error']['code']=='NO_TASK',response.text
    assert rows(conn,t.study_tasks)==before
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.id==plan))==1
