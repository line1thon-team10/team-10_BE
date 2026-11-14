"""Read-only structure candidate inputs preserve identities and learned pages."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.ranges import page_count
from app.services.allocator import allocate
from app.services.plan_inputs import PlanInputError, existing_allocation_input
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


def structure(case, unit=None):
    return {case['material']: {'units': [{'id': unit or str(uuid4()), 'material_id': case['material'],
        'sort_order': 1, 'title': '교체 목차', 'start_page': 1, 'end_page': 20, 'difficulty': 'low', 'in_scope': True}],
        'concepts': []}}


def prepare(conn, case, overrides):
    return existing_allocation_input(conn, case['user'], today=TODAY, timezone_name='Asia/Seoul', subject_ids=(),
        material_structure_overrides=overrides, affected_subject_ids=(case['subject'],))


def test_replaced_unit_uses_material_learning_history_without_mutating_structure(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    task, record = case['old_task'], str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task['id']).values(
        status='partial', scheduled_date=TODAY-timedelta(days=1), starts_at=None, ends_at=None))
    conn.execute(t.study_records.insert().values(id=record, task_id=task['id'], result='partial',
        study_date=TODAY-timedelta(days=1), recorded_at=NOW, updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record, unit_id=case['unit'], start_page=1, end_page=5))
    before = [dict(row) for row in conn.execute(select(t.units)).mappings()]
    overrides = structure(case)
    candidate_id = overrides[case['material']]['units'][0]['id']
    request, supersede, display = prepare(conn, case, overrides)
    result = allocate(request)
    assert result.feasible
    assert supersede == [task['id']]
    assert sum(page_count([part.span]) for placement in result.placements for part in placement.parts) == 15
    assert {part.unit_id for placement in result.placements for part in placement.parts} == {candidate_id}
    assert min(part.span.start for placement in result.placements for part in placement.parts) == 6
    assert [dict(row) for row in conn.execute(select(t.units)).mappings()] == before
    assert page_count(span for unit in display.units for span in unit.ranges) == 20


def test_started_old_unit_remains_fixed_when_new_structure_has_new_identity(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    task = case['old_task']
    conn.execute(t.study_sessions.insert().values(id=str(uuid4()), user_id=case['user'], task_id=task['id'],
        status='ended', started_at=NOW, ended_at=NOW+timedelta(minutes=5), measured_seconds=300,
        study_date=TODAY, created_at=NOW))
    request, supersede, _ = prepare(conn, case, structure(case))
    assert supersede == [] and len(request.fixed) == 1
    assert request.fixed[0].ranges[0].unit_id == case['unit']
    result = allocate(request)
    assert result.feasible and result.placements == ()
    assert conn.scalar(select(t.units.c.id).where(t.units.c.id == case['unit'])) == case['unit']


@pytest.mark.parametrize('mutation', ['foreign_material', 'foreign_unit', 'missing_scope', 'duplicate_unit', 'invalid_concept'])
def test_structure_candidate_cannot_inject_unowned_or_invalid_rows(api_client, domain_connection, addition_case, mutation):
    conn, case = domain_connection, addition_case
    overrides = structure(case)
    rows = overrides[case['material']]['units']
    if mutation == 'foreign_material':
        overrides[str(uuid4())] = overrides.pop(case['material'])
    elif mutation == 'foreign_unit':
        rows[0]['id'] = case['added_unit']
    elif mutation == 'missing_scope':
        del rows[0]['in_scope']
    elif mutation == 'duplicate_unit':
        rows.append(dict(rows[0]))
    else:
        overrides[case['material']]['concepts'] = [{'id':str(uuid4()), 'unit_id':str(uuid4())}]
    with pytest.raises(PlanInputError):
        prepare(conn, case, overrides)


def test_other_subject_future_tasks_remain_fixed(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['added']).values(status='active'))
    task, block = str(uuid4()), str(uuid4())
    conn.execute(t.study_blocks.insert().values(id=block, subject_id=case['added'], unit_id=case['added_unit'],
        task_type='study', created_at=NOW))
    conn.execute(t.study_tasks.insert().values(id=task, plan_id=case['old_task']['plan_id'], subject_id=case['added'],
        block_id=block, task_type='study', title='다른 과목 유지', scheduled_date=TODAY, estimated_minutes=15,
        status='planned', created_at=NOW))
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=task, unit_id=case['added_unit'],
        start_page=1, end_page=6, sort_order=1))
    request, supersede, _ = prepare(conn, case, structure(case))
    assert {fixed.id for fixed in request.fixed} == {task}
    assert supersede == [case['old_task']['id']]
    result = allocate(request)
    assert result.feasible
    assert {placement.subject_id for placement in result.placements} == {case['subject']}
