"""Independent approval atomicity, history preservation and material identity."""
from collections import Counter
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from app.db import schema as t
from tests.e2e.test_independent_range_additions import (
    NOW, TODAY, plan_case, addition_case, case, preview, approve, snapshot,
    placement_pages, pending_pages,
)


def test_page_goal_prefix_is_exact_and_apply_ignores_no_client_changes(api_client, domain_connection, case):
    conn = domain_connection
    before = snapshot(conn); conn.commit()
    response = preview(api_client, case, totalPages=3)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['summary']['goalType'] == 'pages' and value['summary']['totalPages'] == 3
    assert value['summary']['minutesPerDay'] is None
    assert value['conflicts'] == value['leftPages'] == 0
    assert placement_pages(value) == Counter((case['material'], page) for page in [21, 22, 23])
    assert snapshot(conn) == before; conn.commit()
    tampered = approve(api_client, case, value['previewId'], changes={'placements': []})
    assert tampered.status_code == 422
    applied = approve(api_client, case, value['previewId'])
    assert applied.status_code == 200, applied.text
    assert pending_pages(conn, case) == Counter((case['material'], page) for page in range(1, 24))
    after = snapshot(conn); conn.commit()
    assert approve(api_client, case, value['previewId']).json() == applied.json()
    assert snapshot(conn) == after



def test_noncontiguous_learned_pages_are_neither_moved_nor_duplicated(api_client, domain_connection, case):
    conn = domain_connection
    task_id, record_id = case['old_task']['id'], str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task_id).values(status='partial',
        scheduled_date=TODAY-timedelta(days=1), starts_at=None, ends_at=None))
    conn.execute(t.study_records.insert().values(id=record_id, task_id=task_id, result='partial',
        study_date=TODAY-timedelta(days=1), recorded_at=NOW, updated_at=NOW))
    conn.execute(t.study_record_ranges.insert(), [{'id':str(uuid4()), 'record_id':record_id, 'unit_id':case['unit'],
        'start_page':start, 'end_page':end} for start,end in [(1,2),(5,6)]])
    before = snapshot(conn); conn.commit()
    response = preview(api_client, case, **{'from':1, 'to':8, 'totalPages':8})
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['conflicts'] == value['leftPages'] == 0
    assert placement_pages(value) == Counter((case['material'], page) for page in [3,4,7,8])
    assert approve(api_client, case, value['previewId']).status_code == 200
    after = snapshot(conn)
    assert after['study_records'] == before['study_records'] and after['study_record_ranges'] == before['study_record_ranges']
    assert pending_pages(conn, case) == Counter((case['material'], page) for page in range(1,21) if page not in [1,2,5,6])



@pytest.mark.parametrize('status', ['running', 'ended'])
def test_started_task_is_preserved_while_only_new_tail_is_added(api_client, domain_connection, case, status):
    conn = domain_connection
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()), user_id=case['user'], task_id=case['old_task']['id'],
        status=status, started_at=NOW, ended_at=NOW+timedelta(minutes=3) if status=='ended' else None,
        measured_seconds=180 if status=='ended' else 0, study_date=TODAY, created_at=NOW))
    before = snapshot(conn); conn.commit()
    response = preview(api_client, case, **{'from':19,'to':26,'totalPages':8})
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['conflicts'] == value['leftPages'] == 0
    assert placement_pages(value) == Counter((case['material'], page) for page in range(21,27))
    assert approve(api_client, case, value['previewId']).status_code == 200
    after = snapshot(conn)
    assert after['study_sessions'] == before['study_sessions']
    original = dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==case['old_task']['id'])).mappings().one())
    assert original == case['old_task']
    assert pending_pages(conn, case) == Counter((case['material'], page) for page in range(1,27))



def test_pdf_revision_change_invalidates_preview_without_domain_writes(api_client, domain_connection, case):
    response = preview(api_client, case); assert response.status_code == 200, response.text
    conn = domain_connection
    conn.execute(update(t.materials).where(t.materials.c.id==case['material']).values(analysis_version='new-analysis'))
    conn.commit(); before=snapshot(conn);conn.commit()
    response = approve(api_client,case,response.json()['data']['previewId'])
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE'
    assert snapshot(conn)==before



def test_distinct_pdf_same_pages_are_not_consumed_by_existing_learning(api_client, domain_connection, case):
    conn = domain_connection
    material = str(uuid4())
    row=dict(conn.execute(select(t.materials).where(t.materials.c.id==case['material'])).mappings().one())
    conn.execute(t.materials.insert().values(**{**row,'id':material,'sha256':'different-pdf','page_count':6}))
    task_id,record_id=case['old_task']['id'],str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task_id).values(status='partial',
        scheduled_date=TODAY-timedelta(days=1),starts_at=None,ends_at=None))
    conn.execute(t.study_records.insert().values(id=record_id,task_id=task_id,result='partial',
        study_date=TODAY-timedelta(days=1),recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record_id,
        unit_id=case['unit'],start_page=1,end_page=6))
    conn.commit()
    response=preview(api_client,case,**{'materialId':material,'from':1,'to':6})
    assert response.status_code==200,response.text
    value=response.json()['data']
    assert value['conflicts']==value['leftPages']==0
    assert placement_pages(value)==Counter((material,page) for page in range(1,7))
    assert approve(api_client,case,value['previewId']).status_code==200
    assert pending_pages(conn,case)==Counter([*((case['material'],p) for p in range(7,21)),*((material,p) for p in range(1,7))])



def test_foreign_material_and_foreign_approval_never_leak_candidate(api_client, domain_connection, case):
    conn=domain_connection
    material=str(uuid4())
    row=dict(conn.execute(select(t.materials).where(t.materials.c.id==case['material'])).mappings().one())
    conn.execute(t.materials.insert().values(**{**row,'id':material,'subject_id':case['foreign']}));conn.commit()
    before=snapshot(conn);conn.commit()
    response=preview(api_client,case,materialId=material)
    assert response.status_code==404,response.text
    response=preview(api_client,case);assert response.status_code==200,response.text
    response=approve(api_client,{**case,'user':case['other_user']},response.json()['data']['previewId'])
    assert response.status_code==404,response.text
    assert snapshot(conn)==before



def test_database_failure_rolls_back_structure_tasks_and_revision_then_retry_succeeds(api_client, domain_connection, case):
    from app.db import session
    conn=domain_connection
    response=preview(api_client,case);assert response.status_code==200,response.text
    identifier=response.json()['data']['previewId']
    before=snapshot(conn);conn.commit()
    observed=[]
    def reject_task_insert(connection,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('INSERT INTO STUDY_TASKS '):
            observed.append(True)
            raise RuntimeError('independent database write fault')
    event.listen(session.engine,'before_cursor_execute',reject_task_insert)
    try:
        response=approve(api_client,case,identifier)
    finally:
        event.remove(session.engine,'before_cursor_execute',reject_task_insert)
    assert observed and response.status_code==500,response.text
    assert snapshot(conn)==before;conn.commit()
    response=approve(api_client,case,identifier)
    assert response.status_code==200,response.text
    assert pending_pages(conn,case)==Counter((case['material'],page) for page in range(1,27))



