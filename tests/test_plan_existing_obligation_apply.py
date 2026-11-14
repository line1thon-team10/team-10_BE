"""The common apply boundary infers and preserves existing obligation books."""
from datetime import timedelta
from sqlalchemy import select, update
from app.db import schema as t
from app.domain.scheduling import POLICY_VERSION
from app.services.allocator import allocate
from app.services.plan_inputs import existing_allocation_input
from app.services.plan_draft import _basis, _apply, _present
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


def test_common_apply_keeps_existing_obligation_book_and_replays(api_client,domain_connection,addition_case):
    conn,case=domain_connection,addition_case
    original=case['old_task']
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==original['id']).values(task_type='remedial',
        scheduled_date=TODAY,starts_at=None,ends_at=None))
    conn.execute(update(t.study_blocks).where(t.study_blocks.c.id==original['block_id']).values(task_type='remedial'))
    conn.commit()
    domain=PreviewDomain('move_obligation_test',PlanEffect.UPDATE,_basis,_apply)
    service=ProposalService(conn)
    snapshot=service.capture(case['user'],domain=domain,target={'subjectId':case['subject']})
    request,supersede,_=existing_allocation_input(conn,case['user'],today=TODAY,timezone_name='Asia/Seoul',
        subject_ids=(),affected_subject_ids=(case['subject'],),earliest_day=TODAY+timedelta(days=1),
        movable_task_ids=(original['id'],))
    allocation=allocate(request)
    assert allocation.feasible
    _,placements=_present(conn,case['user'],request,allocation,False)
    assert all('source' not in task for task in placements)
    preview=service.create(case['user'],domain=domain,target={'subjectId':case['subject']},snapshot=snapshot,normalized_input={},
        changes={'schemaVersion':'plan-placements-v1','policy':POLICY_VERSION,'timezone':'Asia/Seoul',
            'activateSubjectIds':[],'supersedeIds':supersede,'placements':placements},preview_response={},
        proposal_type='move',description='move existing obligation')
    conn.commit()
    result=service.apply(case['user'],preview['previewId'],domain=domain,target={'subjectId':case['subject']})
    conn.commit()
    rows=conn.execute(select(t.study_tasks).where(t.study_tasks.c.block_id==original['block_id'])).mappings().all()
    assert len(rows)==2
    old=next(row for row in rows if row['id']==original['id'])
    new=next(row for row in rows if row['id']!=original['id'])
    assert old['status']=='carried_over' and new['status']=='planned'
    assert new['origin_task_id']==original['id'] and new['task_type']=='remedial'
    assert service.apply(case['user'],preview['previewId'],domain=domain,target={'subjectId':case['subject']})==result
    assert len(conn.execute(select(t.study_tasks).where(t.study_tasks.c.block_id==original['block_id'])).all())==2
