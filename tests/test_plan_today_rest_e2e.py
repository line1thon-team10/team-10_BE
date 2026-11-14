from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_plan_today_e2e import today_case, copy_task, preview, apply
from tests.test_plan_draft import TODAY, NOW, plan_case


def test_rest_keeps_partial_fact_and_only_places_unfinished_pages_after_today(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    task=case['task']['id'];record=str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task).values(status='partial'))
    conn.execute(t.study_records.insert().values(id=record,task_id=task,result='partial',study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=5))
    before=(conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all());conn.commit()
    response=preview(api_client,case,'rest')
    assert response.status_code==200,response.text
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==task))=='partial'
    conn.commit()
    candidate=response.json()['data']['previewId']
    applied=apply(api_client,case,candidate,'rest')
    assert applied.status_code==200,applied.text
    tasks=conn.execute(select(t.study_tasks).where(t.study_tasks.c.status=='planned')).mappings().all()
    assert tasks and all(TODAY<x['scheduled_date']<TODAY+timedelta(days=3) for x in tasks)
    rows=conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_([x['id'] for x in tasks]))).mappings().all()
    assert sorted(page for row in rows for page in range(row['start_page'],row['end_page']+1))==list(range(6,21))
    assert (conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all())==before
    conn.commit()
    assert apply(api_client,case,candidate,'rest').json()==applied.json()


def test_rest_preserves_started_today_and_can_move_other_unstarted_work(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    original=case['task'];other=copy_task(conn,case,day=TODAY,hour=10,minutes=15,start=1,end=6,kind='remedial')
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=original['id'],status='running',
        started_at=NOW,study_date=TODAY,created_at=NOW))
    conn.commit()
    response=preview(api_client,case,'rest')
    assert response.status_code==200,response.text
    applied=apply(api_client,case,response.json()['data']['previewId'],'rest')
    assert applied.status_code==200,applied.text
    assert dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==original['id'])).mappings().one())==original
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id==other))=='carried_over'
    new=conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id==other)).mappings().all()
    assert new and all(x['task_type']=='remedial' and x['scheduled_date']>TODAY for x in new)
    assert {x['block_id'] for x in new}=={conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id==other))}


def test_rest_allocation_runs_without_user_lock_and_rejects_changed_input(api_client,domain_connection,today_case,monkeypatch):
    from app.services import plan_today
    original=plan_today.allocate
    def change(request):
        with plan_today_service_engine().begin() as conn:
            conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==today_case['task']['id']).values(title='changed'))
        return original(request)
    def plan_today_service_engine():
        from app.db.session import engine
        return engine
    monkeypatch.setattr(plan_today,'allocate',change)
    response=preview(api_client,today_case,'rest')
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE',response.text
    assert domain_connection.scalar(select(t.plans.c.revision))==1


@pytest.mark.parametrize('exam_offset',[0,1])
def test_rest_no_remaining_task_and_exam_tomorrow_boundaries(api_client,domain_connection,today_case,exam_offset):
    conn,case=domain_connection,today_case
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['subject']).values(exam_date=TODAY+timedelta(days=exam_offset)));conn.commit()
    response=preview(api_client,case,'rest')
    assert response.status_code==409 and response.json()['error']['code']=='PLAN_INFEASIBLE',response.text
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['task']['id']).values(status='completed'));conn.commit()
    response=preview(api_client,case,'rest')
    assert response.status_code==400 and response.json()['error']['code']=='NO_TASK',response.text


def test_rest_does_not_split_one_remedial_book_across_short_gaps(api_client,domain_connection,today_case):
    conn,case=domain_connection,today_case
    copy_task(conn,case,day=TODAY,hour=10,minutes=50,kind='remedial')
    for offset in (1,2):
        day=NOW+timedelta(days=offset)
        conn.execute(t.calendar_events.insert().values(id=str(uuid4()),user_id=case['user'],event_type='job',title='busy',
            starts_at=day-timedelta(minutes=30),ends_at=day+timedelta(hours=14),created_at=NOW,updated_at=NOW))
    conn.commit()
    response=preview(api_client,case,'rest')
    assert response.status_code==409 and response.json()['error']['code']=='PLAN_INFEASIBLE',response.text
    assert conn.scalar(select(t.plans.c.revision))==1
