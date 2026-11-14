"""Independent recovery HTTP boundaries, races and transaction rollback."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from threading import Event, Lock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, func, select, update

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_replan_options_e2e import recovery_case, options, apply


def snapshot(conn):
    tables=(t.plans,t.subjects,t.units,t.concepts,t.study_blocks,t.study_tasks,t.study_task_ranges,
            t.study_records,t.study_record_ranges,t.study_sessions,t.plan_change_requests,
            t.plan_proposal_states,t.user_notices)
    return {table.name:[dict(row) for row in conn.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
            for table in tables}


def all_day_event(conn, case, day):
    start=datetime.combine(day,time(8),ZoneInfo('Asia/Seoul')).astimezone(timezone.utc)
    end=datetime.combine(day,time(23),ZoneInfo('Asia/Seoul')).astimezone(timezone.utc)
    conn.execute(t.calendar_events.insert().values(id=str(uuid4()),user_id=case['user'],title='빈 시간 없음',
        event_type='other',starts_at=start,ends_at=end,revision=1,created_at=NOW,updated_at=NOW))


def test_disabled_option_rejects_without_writing_and_another_option_stays_applicable(api_client,domain_connection,recovery_case):
    case,conn=recovery_case,domain_connection
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(emphasis_weight=2))
    conn.commit()
    response=options(api_client,case)
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert {item['key']:item['enabled'] for item in value['options']}=={'A':True,'B':False,'C':True}
    before=snapshot(conn);conn.commit()
    denied=apply(api_client,case,value['previewId'],'B')
    assert denied.status_code==400 and denied.json()['error']['code']=='BAD_KEY',denied.text
    assert snapshot(conn)==before
    conn.commit()
    assert apply(api_client,case,value['previewId'],'A').status_code==200


def test_all_three_can_be_disabled_with_a_real_full_calendar(api_client,domain_connection,recovery_case):
    case,conn=recovery_case,domain_connection
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(emphasis_weight=2))
    for offset in range(3):
        all_day_event(conn,case,TODAY+timedelta(days=offset))
    conn.commit()
    response=options(api_client,case)
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert [item['key'] for item in value['options']]==['A','B','C']
    assert all(item['enabled'] is False and item['totalMinutes']==0 for item in value['options'])
    before=snapshot(conn);conn.commit()
    for key in ('A','B','C'):
        denied=apply(api_client,case,value['previewId'],key)
        assert denied.status_code==400 and denied.json()['error']['code']=='BAD_KEY',denied.text
    assert snapshot(conn)==before


@pytest.mark.parametrize('change',['record','calendar','focus'])
def test_preview_stales_after_actual_learning_calendar_or_focus_changes(api_client,domain_connection,recovery_case,change):
    case,conn=recovery_case,domain_connection
    task=case['tasks'][0]
    if change=='focus':
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(scheduled_date=TODAY,starts_at=None,ends_at=None))
        conn.commit()
    value=options(api_client,case).json()['data']
    if change=='record':
        saved=api_client.post('/api/records',headers=case['headers'],json={'taskId':task['id'],'result':'done'})
        assert saved.status_code==200,saved.text
    elif change=='focus':
        started=api_client.post('/api/study/sessions',headers=case['headers'],json={'taskId':task['id']})
        assert started.status_code==200,started.text
    else:
        all_day_event(conn,case,TODAY)
        conn.commit()
    before=snapshot(conn);conn.commit()
    denied=apply(api_client,case,value['previewId'])
    assert denied.status_code==409 and denied.json()['error']['code']=='PREVIEW_STALE',denied.text
    assert snapshot(conn)==before


def test_foreign_preview_and_unknown_key_never_mutate_the_owner(api_client,domain_connection,recovery_case):
    case,conn=recovery_case,domain_connection
    value=options(api_client,case).json()['data']
    before=snapshot(conn);conn.commit()
    response=api_client.post('/api/plan/replan',headers={'X-Anonymous-Id':case['other_user']},
        json={'previewId':value['previewId'],'key':'A'})
    assert response.status_code==404 and response.json()['error']['code']=='NO_PREVIEW',response.text
    response=apply(api_client,case,value['previewId'],'D')
    assert response.status_code==400 and response.json()['error']['code']=='BAD_KEY',response.text
    assert snapshot(conn)==before


def test_range_insert_failure_rolls_back_scope_tasks_and_acceptance_then_retries(api_client,domain_connection,recovery_case):
    from app.db.session import engine
    case,conn=recovery_case,domain_connection
    for order,(start,end) in enumerate([(1,6),(7,20)],1):
        identifier=str(uuid4())
        conn.execute(t.concepts.insert().values(id=identifier,unit_id=case['unit'],code=identifier,title='비강조 개념',
            sort_order=order,start_page=start,end_page=end,in_scope=True,created_at=NOW,updated_at=NOW))
    conn.commit()
    response=options(api_client,case)
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert next(item for item in value['options'] if item['key']=='B')['enabled']
    before=snapshot(conn);conn.commit()
    fired=[]
    def fail_after_range(_conn,_cursor,_statement,_parameters,context,_many):
        if context.isinsert and getattr(context.compiled.statement.table,'name',None)=='study_task_ranges':
            fired.append(True)
            raise RuntimeError('Injected failure after recovery range insert')
    event.listen(engine,'after_cursor_execute',fail_after_range)
    try:
        failed=apply(api_client,case,value['previewId'],'B')
    finally:
        event.remove(engine,'after_cursor_execute',fail_after_range)
    assert fired and failed.status_code==500,failed.text
    assert snapshot(conn)==before
    conn.commit()
    first=apply(api_client,case,value['previewId'],'B')
    assert first.status_code==200,first.text
    after=snapshot(conn);conn.commit()
    assert apply(api_client,case,value['previewId'],'B').json()==first.json()
    assert snapshot(conn)==after


@pytest.mark.parametrize('same_key',[True,False])
def test_concurrent_option_choices_have_one_winner_and_durable_replay(api_client,domain_connection,recovery_case,same_key):
    from app.db.session import engine
    case,conn=recovery_case,domain_connection
    response=options(api_client,case)
    assert response.status_code==200,response.text
    value=response.json()['data']; preview=value['previewId']
    assert all(item['enabled'] for item in value['options'] if item['key'] in ('A','C'))
    conn.commit()
    entered,release,mutex=Event(),Event(),Lock()
    observed=[]
    def quick_busy(dbapi,_record,_proxy):
        if conn.dialect.name=='sqlite':
            dbapi.execute('PRAGMA busy_timeout=30')
    def held(_conn,_cursor,_statement,_parameters,context,_many):
        if not context.isinsert or getattr(context.compiled.statement.table,'name',None)!='study_task_ranges':
            return
        with mutex:
            if observed:
                return
            observed.append(True)
        entered.set()
        assert release.wait(10),'Test did not release held approval'
    event.listen(engine,'checkout',quick_busy)
    event.listen(engine,'after_cursor_execute',held)
    second_key='A' if same_key else 'C'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner=pool.submit(apply,api_client,case,preview,'A')
            try:
                assert entered.wait(10),'Approval did not insert placement ranges'
                loser=pool.submit(apply,api_client,case,preview,second_key).result(timeout=5)
                assert loser.status_code==409,loser.text
            finally:
                release.set()
            first=winner.result(timeout=5)
            assert first.status_code==200,first.text
    finally:
        release.set()
        event.remove(engine,'after_cursor_execute',held)
        event.remove(engine,'checkout',quick_busy)
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==case['user']))==2
    assert conn.scalar(select(func.count()).select_from(t.user_notices).where(t.user_notices.c.user_id==case['user'],t.user_notices.c.notice_type=='recovery'))==1
    after=snapshot(conn);conn.commit()
    retry=apply(api_client,case,preview,second_key)
    assert retry.status_code==(200 if same_key else 409),retry.text
    if same_key:
        assert retry.json()==first.json()
    else:
        assert retry.json()['error']['code']=='PREVIEW_STALE'
    assert snapshot(conn)==after


def test_completed_preview_replays_after_date_change_without_applying_again(api_client,domain_connection,recovery_case,monkeypatch):
    from app.services import replan_options
    case,conn=recovery_case,domain_connection
    preview=options(api_client,case).json()['data']['previewId']
    first=apply(api_client,case,preview,'C')
    assert first.status_code==200,first.text
    conn.execute(update(t.users).where(t.users.c.id==case['user']).values(demo_date=TODAY+timedelta(days=1)))
    conn.commit()
    def forbidden(*args,**kwargs):
        pytest.fail('A replay must not execute allocation mutations')
    monkeypatch.setattr(replan_options,'_apply_recovery',forbidden)
    assert apply(api_client,case,preview,'C').json()==first.json()
    assert conn.scalar(select(t.plans.c.revision).where(t.plans.c.user_id==case['user']))==2
