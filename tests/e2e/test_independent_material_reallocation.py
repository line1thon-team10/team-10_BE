"""Independent HTTP/DB oracles for approved structure reallocation."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case
from tests.test_plan_addition_e2e import addition_case
from tests.test_plan_draft_e2e import draft, approve
from tests.test_material_structure_e2e import body_for, put


def test_old_learned_pages_are_subtracted_only_from_same_pdf(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    old = case['old_task']
    record, second_material, second_unit = (str(uuid4()) for _ in range(3))
    source = dict(conn.execute(select(t.materials).where(t.materials.c.id == case['material'])).mappings().one())
    conn.execute(t.materials.insert().values(**{**source, 'id': second_material, 'page_count': 5, 'sha256': 'independent-second-pdf'}))
    source = dict(conn.execute(select(t.units).where(t.units.c.id == case['unit'])).mappings().one())
    conn.execute(t.units.insert().values(**{**source, 'id': second_unit, 'material_id': second_material, 'end_page': 5}))
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
    value = response.json()['data']
    assert put(api_client, case, {'previewId': value['previewId']}).status_code == 200
    rows = conn.execute(select(t.study_task_ranges, t.units.c.material_id).join(t.study_tasks,
        t.study_tasks.c.id == t.study_task_ranges.c.task_id).join(t.units, t.units.c.id == t.study_task_ranges.c.unit_id)
        .where(t.study_tasks.c.status == 'planned', t.study_tasks.c.task_type == 'study')).mappings().all()
    actual = {material: sorted(page for row in rows if row['material_id'] == material
        for page in range(row['start_page'], row['end_page'] + 1)) for material in (case['material'], second_material)}
    assert actual == {case['material']: list(range(6, 19)), second_material: list(range(1, 6))}
    assert (conn.execute(select(t.study_records)).all(), conn.execute(select(t.study_record_ranges)).all()) == facts


def test_active_other_subject_allocations_are_unchanged(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    candidate = draft(api_client, case, draftSubjectIds=case['added'])
    assert candidate.status_code == 200, candidate.text
    assert approve(api_client, case, candidate.json()['data']['previewId']).status_code == 200
    other_tasks = conn.execute(select(t.study_tasks).where(t.study_tasks.c.subject_id == case['added'])).all()
    other_ids = [row.id for row in other_tasks]
    other_ranges = conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(other_ids))).all()
    conn.commit()
    body = body_for(api_client, case)
    body['units'][0]['to'] = 10
    candidate = put(api_client, case, body)
    assert candidate.status_code == 200, candidate.text
    applied = put(api_client, case, {'previewId': candidate.json()['data']['previewId']})
    assert applied.status_code == 200, applied.text
    assert conn.execute(select(t.study_tasks).where(t.study_tasks.c.subject_id == case['added'])).all() == other_tasks
    assert conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(other_ids))).all() == other_ranges
