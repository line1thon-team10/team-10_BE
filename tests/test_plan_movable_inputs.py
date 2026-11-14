"""Explicit movement preserves completed pages and independent owed work."""
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from app.services.allocator import allocate
from app.services.plan_inputs import PlanInputError, existing_allocation_input
from tests.test_plan_draft import TODAY, NOW, plan_case
from tests.test_plan_addition_e2e import addition_case


def prepare(conn,case,**options):
    return existing_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',
        subject_ids=(),affected_subject_ids=(case['subject'],),earliest_day=TODAY+timedelta(days=1),**options)


@pytest.mark.parametrize('kind',['study','remedial','review'])
def test_explicit_partial_movement_keeps_actual_pages_and_owed_identity(api_client,domain_connection,addition_case,kind):
    conn,case=domain_connection,addition_case
    task=case['old_task'];record=str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(scheduled_date=TODAY,
        task_type=kind,status='partial',starts_at=None,ends_at=None))
    conn.execute(t.study_records.insert().values(id=record,task_id=task['id'],result='partial',
        study_date=TODAY,recorded_at=NOW,updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()),record_id=record,unit_id=case['unit'],start_page=1,end_page=5))
    before=conn.execute(select(t.study_tasks)).all(),conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all()
    request,supersede,_=prepare(conn,case,movable_task_ids=(task['id'],))
    assert supersede==[task['id']] and request.fixed==()
    if kind=='study':
        assert request.requirements==()
        parts=next(unit.ranges for unit in request.units if unit.id==case['unit'])
    else:
        assert len(request.requirements)==1
        requirement=request.requirements[0]
        assert requirement.indivisible is True
        assert (requirement.id,requirement.kind,requirement.not_before)==(task['id'],kind,TODAY+timedelta(days=1))
        parts=[part.span for part in requirement.parts]
    assert [(part.start,part.end) for part in parts]==[(6,20)]
    result=allocate(request)
    assert result.feasible
    assert all(row.day>=TODAY+timedelta(days=1) for row in result.placements)
    assert before==(conn.execute(select(t.study_tasks)).all(),conn.execute(select(t.study_records)).all(),conn.execute(select(t.study_record_ranges)).all())


@pytest.mark.parametrize('status',['running','paused','ended','auto_stopped'])
def test_started_or_unconsumed_focus_cannot_be_moved(api_client,domain_connection,addition_case,status):
    conn,case=domain_connection,addition_case;task=case['old_task']
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==task['id']).values(scheduled_date=TODAY,starts_at=None,ends_at=None))
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()),user_id=case['user'],task_id=task['id'],
        status=status,started_at=NOW,ended_at=NOW+timedelta(minutes=10) if status in ('ended','auto_stopped') else None,
        measured_seconds=600,study_date=TODAY,created_at=NOW))
    request,supersede,_=prepare(conn,case,movable_task_ids=(task['id'],))
    assert supersede==[] and [row.id for row in request.fixed]==[task['id']]


@pytest.mark.parametrize('bad',['unknown','duplicate'])
def test_invalid_movable_selection_is_rejected(api_client,domain_connection,addition_case,bad):
    ids=(str(uuid4()),) if bad=='unknown' else (addition_case['old_task']['id'],)*2
    with pytest.raises(PlanInputError): prepare(domain_connection,addition_case,movable_task_ids=ids)
