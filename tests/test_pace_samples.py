from datetime import datetime, time, timedelta, timezone
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.repositories.proposals import ProposalRepository
from app.services.pace_samples import collect_samples, encoded
from tests.test_progress_contract import progress_world


def save_placements(w, placements, *, at=None):
    at = at or w.now-timedelta(days=5)
    repo=ProposalRepository(w.conn)
    candidate=repo.create(w.user,kind='initial_plan',target={'scope':'pace-test'},normalized_input={},
        base_date=at.date(),dependency_versions={},changes={'schemaVersion':'plan-placements-v1','placements':placements},
        preview_response={},now=at,proposal_type='initial',description='immutable test placement',
        base_plan_id=w.plan['id'],base_plan_revision=1)
    repo.mark_applied(w.user,candidate['proposal_id'],expected_revision=0,response={'approved':True},now=at)
    return candidate


def sample(w, unit, *, day=None, seconds=1800, pages=10, multiplier=Fraction(1), completed=True, snapshot=True):
    day=day or w.day
    task=w.task((unit,1,pages),done=[(unit,1,pages)] if completed else [],status='completed',day=day)
    session=str(uuid4())
    started=datetime.combine(day,time(8),timezone.utc)
    w.conn.execute(t.study_sessions.insert().values(id=session,user_id=w.user,task_id=task,status='ended',
        started_at=started,ended_at=started+timedelta(seconds=seconds),measured_seconds=seconds,
        study_date=day,created_at=started))
    record=w.conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id==task))
    w.conn.execute(update(t.study_records).where(t.study_records.c.id==record).values(session_id=session))
    placement={'id':task,'subjectId':w.subject['id'],'parts':[{'unitId':unit['id'],'materialId':unit['material_id'],
        'from':1,'to':pages,'baseline':[5,2],'multiplier':encoded(multiplier)}]}
    if snapshot: save_placements(w,[placement])
    return {'task':task,'session':session,'record':record,'placement':placement}


def collect(w):
    user=dict(w.conn.execute(select(t.users).where(t.users.c.id==w.user)).mappings().one())
    subject=dict(w.conn.execute(select(t.subjects).where(t.subjects.c.id==w.subject['id'])).mappings().one())
    return collect_samples(w.conn,user,subject,w.day)


@pytest.mark.parametrize('seconds,eligible',[(1799,False),(1800,True),(1201,False),(1200,True),(1500,False)])
def test_exact_twenty_percent_boundary(progress_world,seconds,eligible):
    w=progress_world;unit=w.unit(1,10)
    for offset in range(3): sample(w,unit,day=w.day-timedelta(days=offset),seconds=seconds)
    measured=collect(w)
    assert measured.days==3 and measured.completed_pages==30
    assert measured.eligible is eligible
    assert measured.proposed==Fraction(seconds,600)


def test_high_difficulty_is_normalized_from_snapshot_not_mutable_unit(progress_world):
    w=progress_world;unit=w.unit(1,10,difficulty='high')
    for offset in range(3): sample(w,unit,day=w.day-timedelta(days=offset),seconds=2250,multiplier=Fraction(3,2))
    w.conn.execute(update(t.units).where(t.units.c.id==unit['id']).values(difficulty='low'))
    measured=collect(w)
    assert measured.weighted_pages==45 and measured.proposed==Fraction(5,2)
    assert measured.ratio==1 and not measured.eligible


@pytest.mark.parametrize('bad',['two_days','auto_stopped','excluded','task_excluded','zero_time','no_pages','legacy','late_snapshot','old_day','foreign_session','review'])
def test_unusable_evidence_cannot_complete_three_day_requirement(progress_world,bad):
    w=progress_world;unit=w.unit(1,10)
    for offset in (0,1): sample(w,unit,day=w.day-timedelta(days=offset))
    last=sample(w,unit,day=w.day-timedelta(days=2),snapshot=bad not in ('legacy','late_snapshot'))
    if bad=='two_days': w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==last['session']).values(study_date=w.day))
    if bad=='auto_stopped': w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==last['session']).values(status='auto_stopped'))
    if bad=='excluded': w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==last['session']).values(excluded_from_calibration=True))
    if bad=='task_excluded': w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==last['task']).values(status='excluded'))
    if bad=='zero_time': w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==last['session']).values(measured_seconds=0))
    if bad=='no_pages': w.conn.execute(t.study_record_ranges.delete().where(t.study_record_ranges.c.record_id==last['record']))
    if bad=='late_snapshot': save_placements(w,[last['placement']],at=w.now)
    if bad=='review': w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==last['task']).values(task_type='review'))
    if bad=='old_day': w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==last['session']).values(study_date=w.day-timedelta(days=3)))
    if bad=='foreign_session':
        other=str(uuid4());w.conn.execute(t.users.insert().values(id=other,created_at=w.now,updated_at=w.now))
        w.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id==last['session']).values(user_id=other))
    assert not collect(w).eligible


def test_same_pdf_overlap_deduplicates_but_distinct_pdf_pages_remain(progress_world):
    w=progress_world;a=w.unit(1,10);b=w.unit(1,10)
    task=w.task((a,1,7),(a,5,10),(b,1,10),done=[(a,1,10),(b,1,10)],status='completed')
    session=str(uuid4());w.conn.execute(t.study_sessions.insert().values(id=session,user_id=w.user,task_id=task,
        status='ended',started_at=w.now,ended_at=w.now+timedelta(hours=1),measured_seconds=3600,study_date=w.day,created_at=w.now))
    record=w.conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id==task))
    w.conn.execute(update(t.study_records).where(t.study_records.c.id==record).values(session_id=session))
    save_placements(w,[{'id':task,'parts':[{'unitId':u['id'],'materialId':u['material_id'],'from':start,'to':end,
        'baseline':[5,2],'multiplier':[3,2] if u==a else [1,1]} for u,start,end in ((a,1,7),(a,5,10),(b,1,10))]}])
    result=collect(w)
    assert len(result.samples)==1 and result.completed_pages==20 and result.weighted_pages==25


@pytest.mark.parametrize('corrupt',[None,'different_block','different_subject','expanded_range','cycle'])
def test_carried_subset_inherits_only_same_block_factors(progress_world,corrupt):
    w=progress_world;unit=w.unit(1,20,difficulty='low')
    source=w.task((unit,1,20),status='carried_over',day=w.day-timedelta(days=3))
    save_placements(w,[{'id':source,'parts':[{'unitId':unit['id'],'materialId':unit['material_id'],
        'from':1,'to':20,'baseline':[5,2],'multiplier':[3,2]}]}])
    child=sample(w,unit,seconds=2250,pages=10,snapshot=False)
    block=w.conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id==source))
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==child['task']).values(origin_task_id=source,
        **({} if corrupt=='different_block' else {'block_id':block})))
    if corrupt=='different_subject':
        subject=w.conn.scalar(select(t.subjects.c.id).where(t.subjects.c.user_id==w.user,t.subjects.c.id!=w.subject['id']))
        w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==source).values(subject_id=subject))
    if corrupt=='expanded_range': w.conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id==child['task']).values(end_page=21))
    if corrupt=='cycle': w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==child['task']).values(origin_task_id=child['task']))
    measured=collect(w)
    if corrupt is None:
        assert len(measured.samples)==1 and measured.weighted_pages==15 and measured.proposed==Fraction(5,2)
    else: assert measured.samples==()


@pytest.mark.parametrize('snapshot_end',[10,21])
def test_carried_snapshot_must_match_original_ranges_before_clipping(progress_world,snapshot_end):
    w=progress_world;unit=w.unit(1,25)
    source=w.task((unit,1,20),status='carried_over',day=w.day-timedelta(days=4))
    save_placements(w,[{'id':source,'parts':[{'unitId':unit['id'],'materialId':unit['material_id'],
        'from':1,'to':snapshot_end,'baseline':[5,2],'multiplier':[3,2]}]}])
    block=w.conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id==source))
    for offset in range(3):
        child=sample(w,unit,day=w.day-timedelta(days=offset),seconds=2250,pages=10,snapshot=False)
        w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==child['task']).values(
            origin_task_id=source,block_id=block))
    # Both altered snapshots cover the child, but neither describes its source.
    assert collect(w).samples==()
