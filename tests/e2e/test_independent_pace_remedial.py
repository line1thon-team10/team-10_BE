"""Actual S02 HTTP production -> measured carried work -> P4 HTTP reader."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.user import User
from app.repositories.users import UserRepository
from tests.test_mastery_rules import mastery_case
from tests.test_remedial_tasks import remedial_case, submit


@pytest.fixture
def measured_supplement(api_client,domain_connection,remedial_case,monkeypatch):
    conn,c=domain_connection,remedial_case
    uid=c['units'][0]['id']
    conn.execute(update(t.subjects).where(t.subjects.c.id==c['subject_id']).values(minutes_per_page=2.5))
    conn.execute(update(t.units).where(t.units.c.id==uid).values(difficulty='mid'))
    conn.commit();submit(api_client,conn,c)
    link=dict(conn.execute(select(t.remedial_obligation_blocks)).mappings().one())
    snapshot=deepcopy(link['allocation_snapshot'])
    assert all(part['baseline']==[5,2] and part['multiplier']==[6,5] for part in snapshot['parts'])
    source=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==snapshot['taskId'])).mappings().one())
    ranges=[dict(row) for row in conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==source['id'])
        .order_by(t.study_task_ranges.c.sort_order)).mappings()]
    assert [(row['start_page'],row['end_page']) for row in ranges]==[(1,3),(8,10)]
    generated=datetime.fromisoformat(snapshot['generatedAt'])
    instant=[generated+timedelta(hours=1)]
    monkeypatch.setattr('app.services.focus.now',lambda:instant[0])
    monkeypatch.setattr('app.routers.today.now_utc',lambda:instant[0])
    tasks=[]
    # The actual source and two same-block subset carries are distinct sessions.
    for offset in range(3):
        day=source['scheduled_date']+timedelta(days=offset)
        if offset==0:
            task=source['id'];pages=6
        else:
            task=str(uuid4());pages=3
            conn.execute(t.study_tasks.insert().values(**{**source,'id':task,'origin_task_id':source['id'],
                'scheduled_date':day,'estimated_minutes':15,'starts_at':None,'ends_at':None,'created_at':instant[0]}))
            conn.execute(t.study_task_ranges.insert().values(**{**ranges[0],'id':str(uuid4()),'task_id':task}))
        conn.execute(update(t.users).where(t.users.c.id==c['user'].id).values(demo_date=day))
        conn.commit()
        response=api_client.post('/api/study/sessions',headers=c['headers'],json={'taskId':task})
        assert response.status_code==200,response.text
        sid=response.json()['data']['id']
        instant[0]+=timedelta(seconds=pages*216) # 3.6 actual minutes/page.
        response=api_client.post('/api/study/sessions/'+sid+'/end',headers=c['headers'])
        assert response.status_code==200,response.text
        response=api_client.post('/api/records',headers=c['headers'],json={'taskId':task,'result':'done','sessionId':sid})
        assert response.status_code==200,response.text
        tasks.append(task)
        instant[0]+=timedelta(hours=1)
    # Changing live settings must not reinterpret the immutable 1.2 multiplier.
    conn.execute(update(t.subjects).where(t.subjects.c.id==c['subject_id']).values(minutes_per_page=5))
    conn.execute(update(t.units).where(t.units.c.id==uid).values(difficulty='high'))
    conn.commit()
    return conn,c,link,snapshot,tasks,instant[0]


def alert(client,case):
    conn,c,*_=case;conn.commit()
    response=client.get('/api/today',headers=c['headers'])
    assert response.status_code==200,response.text
    return response.json()['data']['paceAlert']


def test_http_generated_supplement_and_same_block_subsets_use_saved_factors(api_client,measured_supplement):
    conn,c,link,snapshot,tasks,at=measured_supplement
    value=alert(api_client,measured_supplement)
    assert value and (value['days'],value['sessions'])==(3,3)
    assert (value['perPage'],value['expectedPerPage'],value['ratio'])==(3.6,6.0,0.6)
    assert conn.scalar(select(t.remedial_obligation_blocks.c.allocation_snapshot).where(
        t.remedial_obligation_blocks.c.block_id==link['block_id']))==snapshot
    conn.commit()
    response=api_client.post('/api/plan/pace',headers=c['headers'],json={
        'subjectId':c['subject_id'],'calibrationId':value['calibrationId'],'apply':True})
    assert response.status_code==200,response.text
    assert conn.scalar(select(t.subjects.c.minutes_per_page).where(t.subjects.c.id==c['subject_id']))==3
    assert conn.scalar(select(t.remedial_obligation_blocks.c.allocation_snapshot).where(
        t.remedial_obligation_blocks.c.block_id==link['block_id']))==snapshot


@pytest.mark.parametrize('corrupt',['legacy_null','late','other_task','other_unit','other_material','expanded_range','foreign_owner','different_block'])
def test_invalid_supplement_evidence_cannot_issue_a_three_day_offer(api_client,measured_supplement,corrupt):
    conn,c,link,snapshot,tasks,at=measured_supplement
    value=deepcopy(snapshot)
    if corrupt=='legacy_null':value=None
    if corrupt=='late':value['generatedAt']=(at+timedelta(days=1)).isoformat()
    if corrupt=='other_task':value['taskId']=str(uuid4())
    if corrupt=='other_unit':value['parts'][0]['unitId']=c['units'][1]['id']
    if corrupt=='other_material':value['parts'][0]['materialId']=c['units'][1]['material_id']
    if corrupt=='expanded_range':value['parts'][0]['to']=4
    if corrupt=='foreign_owner':
        user=User.anonymous(now=at);UserRepository(conn).add(user)
        conn.execute(update(t.remedial_obligations).where(t.remedial_obligations.c.id==link['obligation_id']).values(user_id=user.id))
    if corrupt=='different_block':
        block=str(uuid4())
        conn.execute(t.study_blocks.insert().values(id=block,subject_id=c['subject_id'],unit_id=c['units'][0]['id'],task_type='remedial',created_at=at))
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==tasks[-1]).values(block_id=block))
    conn.execute(update(t.remedial_obligation_blocks).where(t.remedial_obligation_blocks.c.block_id==link['block_id']).values(allocation_snapshot=value))
    before=conn.execute(select(t.study_records)).all()
    assert alert(api_client,measured_supplement) is None
    assert conn.execute(select(t.study_records)).all()==before
    assert conn.scalar(select(t.speed_calibrations.c.id).where(t.speed_calibrations.c.subject_id==c['subject_id'])) is None


def test_carried_descendants_cannot_launder_a_snapshot_beyond_original_task_ranges(api_client,measured_supplement,monkeypatch):
    conn,c,link,snapshot,tasks,at=measured_supplement
    source=dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==tasks[0])).mappings().one())
    part=dict(conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==tasks[1])).mappings().one())
    day=conn.scalar(select(t.users.c.demo_date).where(t.users.c.id==c['user'].id))+timedelta(days=1)
    task=str(uuid4())
    conn.execute(t.study_tasks.insert().values(**{**source,'id':task,'origin_task_id':tasks[0],
        'scheduled_date':day,'estimated_minutes':15,'starts_at':None,'ends_at':None,'status':'planned','created_at':at}))
    conn.execute(t.study_task_ranges.insert().values(**{**part,'id':str(uuid4()),'task_id':task,'end_page':4}))
    forged=deepcopy(snapshot);forged['parts'][0]['to']=4
    conn.execute(update(t.remedial_obligation_blocks).where(t.remedial_obligation_blocks.c.block_id==link['block_id']).values(allocation_snapshot=forged))
    conn.execute(update(t.users).where(t.users.c.id==c['user'].id).values(demo_date=day))
    conn.commit();instant=[at+timedelta(hours=1)]
    monkeypatch.setattr('app.services.focus.now',lambda:instant[0])
    monkeypatch.setattr('app.routers.today.now_utc',lambda:instant[0])
    started=api_client.post('/api/study/sessions',headers=c['headers'],json={'taskId':task})
    assert started.status_code==200,started.text
    sid=started.json()['data']['id'];instant[0]+=timedelta(seconds=864)
    assert api_client.post('/api/study/sessions/'+sid+'/end',headers=c['headers']).status_code==200
    recorded=api_client.post('/api/records',headers=c['headers'],json={'taskId':task,'result':'done','sessionId':sid})
    assert recorded.status_code==200,recorded.text
    # The original task owns 1..3 and 8..10, so page 4 is never inherited evidence.
    assert alert(api_client,measured_supplement) is None
