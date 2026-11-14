"""Reopened ancestor pages remain owed across protected/completed carryovers."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from app.db import schema as t
from app.domain.scheduling import POLICY_VERSION
from app.services.allocator import allocate
from app.services.plan_inputs import existing_allocation_input, PendingObligations
from app.services.plan_draft import _basis, _apply, _present
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


def chain(conn, case, *, child_status='planned', corrected_end=2, kind='remedial'):
    root = case['old_task']
    conn.execute(update(t.study_blocks).where(t.study_blocks.c.id == root['block_id']).values(task_type=kind))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == root['id']).values(task_type=kind,
        status='carried_over', scheduled_date=TODAY-timedelta(days=1), starts_at=None, ends_at=None))
    child = str(uuid4())
    conn.execute(t.study_tasks.insert().values(**{**root, 'id': child, 'origin_task_id': root['id'],
        'status': child_status, 'task_type': kind, 'scheduled_date': TODAY, 'starts_at': None, 'ends_at': None,
        'estimated_minutes': 30}))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=child, unit_id=case['unit'],
        start_page=9, end_page=20, sort_order=1))
    for task, start, end, result in [(root['id'], 1, corrected_end, 'partial')] + (
            [(child, 9, 20, 'completed')] if child_status == 'completed' else []):
        record = str(uuid4())
        conn.execute(t.study_records.insert().values(id=record, task_id=task, result=result,
            study_date=TODAY, recorded_at=NOW, updated_at=NOW))
        conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record,
            unit_id=case['unit'], start_page=start, end_page=end))
    return child


def prepare(conn, case, **kwargs):
    return existing_allocation_input(conn, case['user'], today=TODAY, timezone_name='Asia/Seoul',
        subject_ids=(), affected_subject_ids=(case['subject'],), earliest_day=TODAY+timedelta(days=1), **kwargs)


@pytest.mark.parametrize('session_status', [None, 'running', 'paused', 'ended', 'auto_stopped'])
def test_reopened_pages_cannot_disappear_behind_fixed_live_child(api_client, domain_connection, addition_case, session_status):
    conn, case = domain_connection, addition_case
    child = chain(conn, case)
    if session_status:
        conn.execute(t.study_sessions.insert().values(id=str(uuid4()), user_id=case['user'], task_id=child,
            status=session_status, started_at=NOW, measured_seconds=600, study_date=TODAY, created_at=NOW))
    before = conn.execute(select(t.study_tasks)).all()
    with pytest.raises(PendingObligations, match='기록 정정'):
        prepare(conn, case, movable_task_ids=(child,) if session_status else ())
    assert conn.execute(select(t.study_tasks)).all() == before


@pytest.mark.parametrize('kind', ['remedial', 'review'])
def test_movable_child_absorbs_reopened_ancestor_pages_once(api_client, domain_connection, addition_case, kind):
    conn, case = domain_connection, addition_case
    child = chain(conn, case, kind=kind)
    request, supersede, _ = prepare(conn, case, movable_task_ids=(child,))
    assert supersede == [child]
    assert len(request.requirements) == 1
    requirement = request.requirements[0]
    assert requirement.id == child and requirement.indivisible
    assert [(part.span.start, part.span.end) for part in requirement.parts] == [(3, 20)]
    result = allocate(request)
    assert result.feasible
    owed = [placement for placement in result.placements if placement.kind == kind]
    assert len(owed) == 1


@pytest.mark.parametrize('kind', ['remedial', 'review'])
def test_completed_child_reopens_original_book_and_preserves_records(api_client, domain_connection, addition_case, kind):
    conn, case = domain_connection, addition_case
    child = chain(conn, case, child_status='completed', kind=kind)
    conn.commit()
    domain = PreviewDomain('residual_test', PlanEffect.UPDATE, _basis, _apply)
    service = ProposalService(conn)
    target = {'subjectId': case['subject']}
    snapshot = service.capture(case['user'], domain=domain, target=target)
    request, supersede, _ = prepare(conn, case)
    root = case['old_task']
    assert supersede == [root['id']]
    assert [(part.span.start, part.span.end) for part in request.requirements[0].parts] == [(3, 8)]
    allocation = allocate(request)
    assert allocation.feasible
    _, placements = _present(conn, case['user'], request, allocation, False)
    before_records = conn.execute(select(t.study_records)).all(), conn.execute(select(t.study_record_ranges)).all()
    preview = service.create(case['user'], domain=domain, target=target, snapshot=snapshot, normalized_input={},
        changes={'schemaVersion': 'plan-placements-v1', 'policy': POLICY_VERSION, 'timezone': 'Asia/Seoul',
                 'activateSubjectIds': [], 'supersedeIds': supersede, 'placements': placements},
        preview_response={}, proposal_type='residual', description='restore corrected residual')
    conn.commit()
    result = service.apply(case['user'], preview['previewId'], domain=domain, target=target)
    conn.commit()
    rows = conn.execute(select(t.study_tasks).where(t.study_tasks.c.block_id == root['block_id'])).mappings().all()
    assert len(rows) == 3
    live = [row for row in rows if row['status'] == 'planned']
    assert len(live) == 1 and live[0]['origin_task_id'] == root['id']
    assert next(row for row in rows if row['id'] == child)['status'] == 'completed'
    assert before_records == (conn.execute(select(t.study_records)).all(), conn.execute(select(t.study_record_ranges)).all())
    assert service.apply(case['user'], preview['previewId'], domain=domain, target=target) == result


def test_full_block_actual_union_does_not_recreate_completed_work(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    chain(conn, case, child_status='completed', corrected_end=8)
    request, supersede, _ = prepare(conn, case)
    assert request.requirements == () and supersede == []
