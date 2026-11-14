"""Candidate subject inputs cannot mutate facts or pull tomorrow's work into today."""
from datetime import timedelta
from fractions import Fraction
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from app.db import schema as t
from app.services.allocator import allocate
from app.services.plan_inputs import PlanInputError, existing_allocation_input
from tests.test_plan_draft import TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


def prepare(conn, case, **options):
    return existing_allocation_input(conn, case['user'], today=TODAY, timezone_name='Asia/Seoul',
        subject_ids=(), affected_subject_ids=(case['subject'],), **options)


def test_subject_candidate_is_read_only_and_today_remains_fixed(api_client, domain_connection, addition_case):
    conn,case=domain_connection,addition_case
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==case['old_task']['id']).values(
        scheduled_date=TODAY, starts_at=None, ends_at=None))
    conn.commit()
    before=conn.execute(select(t.subjects)).all(),conn.execute(select(t.study_tasks)).all()
    request,supersede,display=prepare(conn,case,earliest_day=TODAY+timedelta(days=1),
        subject_overrides={case['subject']:{'exam_date':TODAY+timedelta(days=5),'target_grade':'B+','target_score':80,'minutes_per_page':Fraction(3)}})
    subject=next(s for s in request.subjects if s.id==case['subject'])
    assert (subject.exam_date,subject.target_score,subject.minutes_per_page)==(TODAY+timedelta(days=5),80,3)
    assert request.start_date==TODAY+timedelta(days=1) and display.start_date==TODAY
    assert [task.id for task in request.fixed]==[case['old_task']['id']] and supersede==[]
    result=allocate(request)
    assert result.feasible and result.placements==()
    assert before==(conn.execute(select(t.subjects)).all(),conn.execute(select(t.study_tasks)).all())


@pytest.mark.parametrize('change',[{'exam_date':TODAY},{'exam_date':'2026-12-01'},{'target_score':101},
    {'target_score':float('nan')},{'minutes_per_page':0},{'minutes_per_page':True},{'target_grade':' '},{'status':'archived'}])
def test_invalid_subject_candidate_is_rejected(api_client,domain_connection,addition_case,change):
    with pytest.raises(PlanInputError):
        prepare(domain_connection,addition_case,subject_overrides={addition_case['subject']:change})


def test_foreign_subject_and_backdated_effective_day_rejected(api_client,domain_connection,addition_case):
    with pytest.raises(PlanInputError):
        prepare(domain_connection,addition_case,subject_overrides={addition_case['foreign']:{'target_score':80}})
    with pytest.raises(PlanInputError):
        prepare(domain_connection,addition_case,earliest_day=TODAY-timedelta(days=1))
