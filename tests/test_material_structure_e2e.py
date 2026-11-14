from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from app.db import schema as t
from tests.test_units_contract import unit_case, add_unit, add_concept
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case


def body_for(client, case, *, subject=None, material=None):
    response = client.get('/api/units', headers={'X-Anonymous-Id': case['user']}, params={
        'subjectId': subject or case['subject'], 'materialId': material or case['material']})
    assert response.status_code == 200, response.text
    body = response.json()['data']
    body.pop('file')
    for unit in body['units']:
        unit.pop('quizless')
        unit.pop('evidence')
    return body


def put(client, case, body):
    return client.put('/api/units', headers={'X-Anonymous-Id': case['user']}, json=body)


def test_draft_put_patch_download_structure_is_immediate(api_client, domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    unit = add_unit(conn, case)
    concept = add_concept(conn, unit)
    conn.commit()
    body = body_for(api_client, case)
    body['units'][0].update(title='수정 제목', difficulty='high')
    response = put(api_client, case, body)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['applied'] and data['previewId'] is None
    assert data['structure']['units'][0]['id'] == unit
    assert conn.execute(select(t.plan_proposals)).first() is None


def test_new_draft_subject_edits_do_not_touch_existing_plan(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    tasks = conn.execute(select(t.study_tasks)).all()
    revision = conn.scalar(select(t.plans.c.revision))
    conn.commit()
    body = body_for(api_client, case, subject=case['added'], material=case['added_material'])
    body['units'][0]['title'] = '새 draft 목차'
    response = put(api_client, case, body)
    assert response.status_code == 200 and response.json()['data']['applied'], response.text
    assert response.json()['data']['previewId'] is None
    assert conn.execute(select(t.study_tasks)).all() == tasks
    assert conn.scalar(select(t.plans.c.revision)) == revision


def test_approved_structure_preview_is_read_only_then_applies_and_replays(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    before_units = conn.execute(select(t.units)).all()
    before_tasks = conn.execute(select(t.study_tasks)).all()
    conn.commit()
    body = body_for(api_client, case)
    body['units'][0]['to'] = 18
    response = put(api_client, case, body)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['applied'] is False and data['previewId']
    new_unit = data['structure']['units'][0]['id']
    assert new_unit != case['unit'] and data['structure']['units'][0]['quizless']
    assert conn.execute(select(t.units)).all() == before_units
    assert conn.execute(select(t.study_tasks)).all() == before_tasks
    conn.commit()
    assert put(api_client, case, body).json() == response.json()
    result = put(api_client, case, {'previewId': data['previewId']})
    assert result.status_code == 200, result.text
    assert result.json()['data']['applied'] is True
    assert result.json()['data']['structure'] == data['structure']
    assert conn.scalar(select(t.units.c.retired_at).where(t.units.c.id == case['unit'])) is not None
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == case['old_task']['id'])) == 'carried_over'
    assert conn.scalar(select(t.plans.c.revision)) == 2
    tasks = conn.execute(select(t.study_tasks)).all()
    conn.commit()
    assert put(api_client, case, {'previewId': data['previewId']}).json() == result.json()
    assert conn.execute(select(t.study_tasks)).all() == tasks


@pytest.mark.parametrize('change', ['date', 'unit', 'plan_revision'])
def test_changed_basis_rejects_preview_without_structure_mutation(api_client, domain_connection, addition_case, change):
    from datetime import timedelta
    conn, case = domain_connection, addition_case
    body = body_for(api_client, case)
    body['units'][0]['to'] = 18
    response = put(api_client, case, body)
    assert response.status_code == 200, response.text
    preview = response.json()['data']['previewId']
    if change == 'date':
        conn.execute(update(t.users).where(t.users.c.id == case['user']).values(demo_date=TODAY+timedelta(days=1)))
    elif change == 'unit':
        conn.execute(update(t.units).where(t.units.c.id == case['unit']).values(title='Another tab'))
    else:
        conn.execute(update(t.plans).where(t.plans.c.user_id == case['user']).values(revision=2))
    conn.commit()
    before = conn.execute(select(t.units)).all()
    conn.commit()
    rejected = put(api_client, case, {'previewId': preview})
    assert rejected.status_code == 409 and rejected.json()['error']['code'] == 'PREVIEW_STALE', rejected.text
    assert conn.execute(select(t.units)).all() == before


def test_all_scope_exclusion_and_foreign_preview_are_rejected(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    body = body_for(api_client, case)
    body['units'][0]['inExam'] = False
    response = put(api_client, case, body)
    assert response.status_code == 409 and response.json()['error']['code'] == 'PLAN_INFEASIBLE', response.text
    body['units'][0]['inExam'] = True
    body['units'][0]['title'] = 'Candidate'
    candidate = put(api_client, case, body).json()['data']['previewId']
    rejected = api_client.put('/api/units', headers={'X-Anonymous-Id': case['other_user']}, json={'previewId': candidate})
    assert rejected.status_code == 404


def test_approval_write_failure_rolls_back_retirement_and_can_retry(api_client, domain_connection, addition_case):
    from app.db.session import engine
    conn, case = domain_connection, addition_case
    body = body_for(api_client, case)
    body['units'][0]['to'] = 18
    preview = put(api_client, case, body).json()['data']['previewId']
    before = (conn.execute(select(t.units)).all(), conn.execute(select(t.study_tasks)).all())
    conn.commit()
    def fail(db_conn, cursor, statement, parameters, context, many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'study_task_ranges':
            raise RuntimeError('Fail after structural edits')
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        response = put(api_client, case, {'previewId': preview})
        assert response.status_code == 500
    finally:
        event.remove(engine, 'before_cursor_execute', fail)
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.study_tasks)).all()) == before
    conn.commit()
    assert put(api_client, case, {'previewId': preview}).status_code == 200


@pytest.mark.parametrize('mutation', ['bool_page', 'unknown_output', 'empty', 'huge_no'])
def test_public_write_contract_rejects_invalid_fields(api_client, domain_connection, unit_case, mutation):
    conn, case = domain_connection, unit_case
    unit = add_unit(conn, case)
    concept = add_concept(conn, unit)
    conn.commit()
    body = body_for(api_client, case)
    if mutation == 'huge_no': body['units'][0]['no'] = 2_147_483_648
    if mutation == 'bool_page': body['units'][0]['from'] = True
    if mutation == 'unknown_output': body['units'][0]['quizless'] = False
    if mutation == 'empty': body['units'] = []
    result = put(api_client, case, body)
    assert result.status_code == (400 if mutation == 'empty' else 422), result.text


def test_changed_unit_preserves_recorded_pages_and_only_allocates_remainder(api_client, domain_connection, addition_case):
    from datetime import timedelta
    conn, case = domain_connection, addition_case
    record = str(uuid4())
    old = case['old_task']
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == old['id']).values(
        status='partial', scheduled_date=TODAY-timedelta(days=1), starts_at=None, ends_at=None))
    conn.execute(t.study_records.insert().values(id=record, task_id=old['id'], result='partial',
        study_date=TODAY-timedelta(days=1), recorded_at=NOW, updated_at=NOW))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record,
        unit_id=case['unit'], start_page=1, end_page=5))
    facts = (conn.execute(select(t.study_records)).all(), conn.execute(select(t.study_record_ranges)).all())
    conn.commit()
    body = body_for(api_client, case)
    body['units'][0]['to'] = 18
    response = put(api_client, case, body)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert put(api_client, case, {'previewId': data['previewId']}).status_code == 200
    assert (conn.execute(select(t.study_records)).all(), conn.execute(select(t.study_record_ranges)).all()) == facts
    ranges = conn.execute(select(t.study_task_ranges).join(t.study_tasks,
        t.study_tasks.c.id == t.study_task_ranges.c.task_id).where(t.study_tasks.c.status == 'planned')).mappings().all()
    assert sorted(page for row in ranges for page in range(row['start_page'], row['end_page'] + 1)) == list(range(6, 19))
    assert {row['unit_id'] for row in ranges} == {data['structure']['units'][0]['id']}


def test_second_tab_preview_becomes_stale_after_first_approval(api_client, domain_connection, addition_case):
    case = addition_case
    first = body_for(api_client, case)
    second = deepcopy(first)
    first['units'][0]['to'] = 18
    second['units'][0]['to'] = 16
    one = put(api_client, case, first).json()['data']['previewId']
    two = put(api_client, case, second).json()['data']['previewId']
    assert put(api_client, case, {'previewId': one}).status_code == 200
    rejected = put(api_client, case, {'previewId': two})
    assert rejected.status_code == 409 and rejected.json()['error']['code'] == 'PREVIEW_STALE'


def test_invalid_legacy_page_structure_is_a_domain_error_not_server_failure(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    material = dict(conn.execute(select(t.materials).where(t.materials.c.id == case['material'])).mappings().one())
    identifier = str(uuid4())
    conn.execute(t.materials.insert().values(**{**material, 'id': identifier, 'sha256': 'legacy-time'}))
    conn.execute(t.units.insert().values(id=str(uuid4()), material_id=identifier, code='legacy', title='Legacy',
        sort_order=1, quantity_basis='time', estimated_minutes=20, created_at=NOW, updated_at=NOW))
    conn.commit()
    body = body_for(api_client, case)
    body['units'][0]['title'] = 'Edit'
    result = put(api_client, case, body)
    assert result.status_code == 422 and result.json()['error']['code'] == 'INVALID_INPUT', result.text
