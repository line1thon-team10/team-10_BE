"""Later previews retain today's old target after same-day approved goal edits."""
from datetime import timedelta
from sqlalchemy import select, update
from app.db import schema as t
from app.repositories.proposals import ProposalRepository
from app.services.plan_inputs import existing_allocation_input
from tests.test_plan_draft import TODAY, NOW, plan_case
from tests.test_plan_addition_e2e import addition_case


def prepare(conn,case,**options):
    return existing_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',
        subject_ids=(),affected_subject_ids=(case['subject'],),**options)[0]


def applied(conn,case,previous,current,offset=0):
    repo=ProposalRepository(conn)
    value=repo.create(case['user'],kind='subject_settings',target={'subjectId':case['subject']},
        normalized_input={'target':current},base_date=TODAY,dependency_versions={},
        changes={'subjectId':case['subject'],'values':{'target_score':current},
            'goalEffectiveDate':(TODAY+timedelta(days=1)).isoformat(),'previousTargetScore':previous},
        preview_response={},now=NOW+timedelta(seconds=offset),proposal_type='subject',description='test',
        base_plan_id=case['old_task']['plan_id'],base_plan_revision=1)
    repo.mark_applied(case['user'],value['proposal_id'],expected_revision=0,response={'applied':True},now=NOW+timedelta(seconds=offset+1))
    conn.execute(update(t.subjects).where(t.subjects.c.id==case['subject']).values(target_score=current))


def test_same_day_followup_and_multiple_changes_keep_original_target(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    applied(conn,case,90,80)
    applied(conn,case,80,70,offset=2)
    request=prepare(conn,case)
    subject=next(s for s in request.subjects if s.id==case['subject'])
    assert subject.target_on(TODAY)==90 and subject.target_on(TODAY+timedelta(days=1))==70
    candidate=prepare(conn,case,earliest_day=TODAY+timedelta(days=1),subject_overrides={case['subject']:{'target_score':60}})
    subject=next(s for s in candidate.subjects if s.id==case['subject'])
    assert subject.target_on(TODAY)==90 and subject.target_on(TODAY+timedelta(days=1))==60
    assert conn.scalar(select(t.subjects.c.target_score).where(t.subjects.c.id==case['subject']))==70


def test_unapproved_goal_does_not_override_current_weights(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    request=prepare(conn,case,earliest_day=TODAY+timedelta(days=1),subject_overrides={case['subject']:{'target_score':80}})
    subject=next(s for s in request.subjects if s.id==case['subject'])
    assert subject.target_on(TODAY)==90 and subject.target_on(TODAY+timedelta(days=1))==80
    subject=next(s for s in prepare(conn,case).subjects if s.id==case['subject'])
    assert subject.target_on(TODAY)==90 and subject.target_on(TODAY+timedelta(days=1))==90
