"""Unassigned remedial work survives previews and links each newly allocated book."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update, func
from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case
from tests.test_plan_draft_e2e import draft, approve
from tests.test_material_structure_e2e import body_for, put


def pending(conn,case,end=20):
    quiz,obligation=str(uuid4()),str(uuid4())
    conn.execute(t.quiz_sessions.insert().values(id=quiz,user_id=case['user'],subject_id=case['subject'],
        type='practice',quiz_date=TODAY,status='submitted',selected_difficulty='low',created_at=NOW,finalized_at=NOW))
    conn.execute(t.remedial_obligations.insert().values(id=obligation,user_id=case['user'],unit_id=case['unit'],
        quiz_session_id=quiz,scope_revision='a'*64,required_ranges=[[1,end]],not_before=TODAY,created_at=NOW))
    conn.commit()
    return obligation


def test_addition_allocates_every_pending_page_and_links_split_books_once(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    conn.execute(update(t.materials).where(t.materials.c.id==case['material']).values(page_count=60))
    conn.execute(update(t.units).where(t.units.c.id==case['unit']).values(end_page=60))
    obligation=pending(conn,case,60)
    preview=draft(api_client,case,draftSubjectIds=case['added'])
    assert preview.status_code==200,preview.text
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligation_blocks))==0
    conn.commit()
    response=approve(api_client,case,preview.json()['data']['previewId'])
    assert response.status_code==200,response.text
    links=list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id).where(t.remedial_obligation_blocks.c.obligation_id==obligation)))
    assert len(links)>=3
    snapshots=list(conn.scalars(select(t.remedial_obligation_blocks.c.allocation_snapshot).where(
        t.remedial_obligation_blocks.c.obligation_id==obligation)))
    assert all(snapshot['schemaVersion']=='remedial-allocation-v1' and snapshot['generatedAt'] for snapshot in snapshots)
    for snapshot in snapshots:
        from fractions import Fraction
        task=conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==snapshot['taskId'])).mappings().one()
        assert snapshot['date']==task['scheduled_date'].isoformat()
        measured=sum((Fraction(*part['baseline'])*Fraction(*part['multiplier'])*(part['to']-part['from']+1)
            for part in snapshot['parts']),Fraction(0))
        assert Fraction(*snapshot['end'])-Fraction(*snapshot['start'])==measured
        assert all(part['baseline']==[5,2] and part['multiplier']==[1,1] for part in snapshot['parts'])
    rows=conn.execute(select(t.study_task_ranges).join(t.study_tasks,t.study_tasks.c.id==t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.block_id.in_(links),t.study_tasks.c.status=='planned')).mappings().all()
    assert sorted(page for row in rows for page in range(row['start_page'],row['end_page']+1))==list(range(1,61))
    count=conn.scalar(select(func.count()).select_from(t.study_blocks));conn.commit()
    assert approve(api_client,case,preview.json()['data']['previewId']).json()==response.json()
    assert conn.scalar(select(func.count()).select_from(t.study_blocks))==count


def test_new_pending_obligation_after_preview_is_stale(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    preview=draft(api_client,case,draftSubjectIds=case['added'])
    assert preview.status_code==200
    pending(conn,case)
    response=approve(api_client,case,preview.json()['data']['previewId'])
    assert response.status_code==409 and response.json()['error']['code']=='PREVIEW_STALE'
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligation_blocks))==0


def test_material_structure_reallocation_keeps_original_retired_obligation_identity(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    obligation=pending(conn,case)
    body=body_for(api_client,case)
    body['units'][0]['to']=18
    response=put(api_client,case,body)
    assert response.status_code==200,response.text
    assert put(api_client,case,{'previewId':response.json()['data']['previewId']}).status_code==200
    links=list(conn.scalars(select(t.remedial_obligation_blocks.c.block_id).where(t.remedial_obligation_blocks.c.obligation_id==obligation)))
    rows=conn.execute(select(t.study_task_ranges).join(t.study_tasks,t.study_tasks.c.id==t.study_task_ranges.c.task_id)
        .where(t.study_tasks.c.block_id.in_(links))).mappings().all()
    assert {row['unit_id'] for row in rows}=={case['unit']}
    assert sorted(page for row in rows for page in range(row['start_page'],row['end_page']+1))==list(range(1,21))
    assert conn.scalar(select(t.units.c.retired_at).where(t.units.c.id==case['unit'])) is not None


def test_link_write_failure_rolls_back_all_new_tasks_and_can_retry(api_client,domain_connection,addition_case,monkeypatch):
    from sqlalchemy.engine import Connection
    conn,case=domain_connection,addition_case
    pending(conn,case)
    preview=draft(api_client,case,draftSubjectIds=case['added'])
    assert preview.status_code==200
    before=(conn.execute(select(t.study_tasks)).all(),conn.execute(select(t.study_blocks)).all())
    conn.commit()
    execute=Connection.execute
    def fail_link(self,statement,*args,**kwargs):
        if getattr(statement,'is_insert',False) and getattr(getattr(statement,'table',None),'name',None)=='remedial_obligation_blocks':
            raise RuntimeError('injected link storage failure')
        return execute(self,statement,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Connection,'execute',fail_link)
        response=approve(api_client,case,preview.json()['data']['previewId'])
    assert response.status_code==500
    assert before==(conn.execute(select(t.study_tasks)).all(),conn.execute(select(t.study_blocks)).all())
    conn.commit()
    assert approve(api_client,case,preview.json()['data']['previewId']).status_code==200


def test_cross_owner_obligation_evidence_fails_closed(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    obligation=pending(conn,case)
    quiz=conn.scalar(select(t.remedial_obligations.c.quiz_session_id).where(t.remedial_obligations.c.id==obligation))
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id==quiz).values(user_id=case['other_user']))
    conn.commit()
    response=draft(api_client,case,draftSubjectIds=case['added'])
    assert response.status_code==422,response.text
    assert conn.scalar(select(func.count()).select_from(t.remedial_obligation_blocks))==0
