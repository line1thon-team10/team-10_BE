"""P4 consumes S02 immutable factors, including same-block carried subsets."""
from datetime import timedelta
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import select,update
from app.db import schema as t
from tests.test_progress_contract import progress_world
from tests.test_pace_samples import collect,sample


def obligation(w,unit):
    identifier=str(uuid4())
    quiz=w.conn.scalar(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id==w.user))
    w.conn.execute(t.remedial_obligations.insert().values(id=identifier,user_id=w.user,unit_id=unit['id'],
        quiz_session_id=quiz,scope_revision='fixed',required_ranges=[[1,20]],not_before=w.day-timedelta(days=4),
        created_at=w.now-timedelta(days=5)))
    return identifier


def link(w,unit,oid,task):
    block=w.conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id==task))
    value={'schemaVersion':'remedial-allocation-v1','generatedAt':(w.now-timedelta(days=4)).isoformat(),
        'taskId':task,'parts':[{'unitId':unit['id'],'materialId':unit['material_id'],'from':1,'to':20,
            'baseline':[5,2],'multiplier':[3,2]}]}
    w.conn.execute(t.remedial_obligation_blocks.insert().values(obligation_id=oid,block_id=block,allocation_snapshot=value))
    return block,value


@pytest.mark.parametrize('corrupt',[None,'legacy','late','other_task','other_unit','bad_timestamp','foreign_owner','different_block','expanded'])
def test_supplement_snapshot_requires_owned_original_and_valid_carried_subset(progress_world,corrupt):
    w=progress_world;unit=w.unit(1,25,difficulty='low');oid=obligation(w,unit)
    source=w.task((unit,1,20),kind='remedial',status='carried_over',day=w.day-timedelta(days=3))
    block,value=link(w,unit,oid,source)
    for offset in range(3):
        evidence=sample(w,unit,day=w.day-timedelta(days=offset),seconds=2250,pages=10,snapshot=False)
        w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==evidence['task']).values(
            task_type='remedial',origin_task_id=source,**({} if corrupt=='different_block' else {'block_id':block})))
        if corrupt=='expanded':
            w.conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id==evidence['task']).values(end_page=21))
    if corrupt=='legacy':value=None
    if corrupt=='late':value['generatedAt']=(w.now+timedelta(days=1)).isoformat()
    if corrupt=='other_task':value['taskId']=str(uuid4())
    if corrupt=='other_unit':value['parts'][0]['unitId']=str(uuid4())
    if corrupt=='bad_timestamp':value['generatedAt']='2026-11-01'
    if corrupt=='foreign_owner':
        other=str(uuid4());w.conn.execute(t.users.insert().values(id=other,created_at=w.now,updated_at=w.now))
        w.conn.execute(update(t.remedial_obligations).where(t.remedial_obligations.c.id==oid).values(user_id=other))
    w.conn.execute(update(t.remedial_obligation_blocks).where(t.remedial_obligation_blocks.c.block_id==block).values(allocation_snapshot=value))
    result=collect(w)
    if corrupt is None:
        assert len(result.samples)==3 and result.completed_pages==30
        assert result.weighted_pages==45 and result.proposed==Fraction(5,2) and result.ratio==1
    else:assert result.samples==()


def test_real_supplement_producer_snapshot_is_read_without_current_speed_reinterpretation(api_client,domain_connection,remedial_case):
    from app.services.pace_samples import placement_snapshots
    from tests.test_remedial_tasks import submit
    conn,c=domain_connection,remedial_case
    submit(api_client,conn,c)
    links=conn.execute(select(t.remedial_obligation_blocks.c.allocation_snapshot)).scalars().all()
    assert links
    conn.execute(update(t.subjects).where(t.subjects.c.id==c['subject_id']).values(minutes_per_page=99))
    snapshots=placement_snapshots(conn,c['user'].id)
    for value in links:
        actual=snapshots[value['taskId']][-1][1]
        assert actual=={'id':value['taskId'],'parts':value['parts']}


from tests.test_mastery_rules import mastery_case
from tests.test_remedial_tasks import remedial_case
