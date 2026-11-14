"""Explicit draft scope survives resplit and stale-source recomputation."""
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_plan_resplit_e2e import plan_case, call, prepare
from tests.e2e.test_independent_resplit_grounding import model


@pytest.mark.parametrize('stale', [False, True])
def test_initial_resplit_never_activates_unselected_draft_subject(api_client, domain_connection, plan_case, model, stale):
    conn, case = domain_connection, plan_case
    conn.execute(update(t.subjects).where(t.subjects.c.id == case['active']).values(status='archived'))
    other, material, unit = [str(uuid4()) for _ in range(3)]
    subject_row = dict(conn.execute(select(t.subjects).where(t.subjects.c.id == case['subject'])).mappings().one())
    material_row = dict(conn.execute(select(t.materials).where(t.materials.c.id == case['material'])).mappings().one())
    unit_row = dict(conn.execute(select(t.units).where(t.units.c.id == case['unit'])).mappings().one())
    conn.execute(t.subjects.insert().values(**{**subject_row, 'id': other, 'code': other, 'name': '선택 안 한 과목'}))
    conn.execute(t.materials.insert().values(**{**material_row, 'id': material, 'subject_id': other, 'sha256': material, 'page_count': 6}))
    conn.execute(t.units.insert().values(**{**unit_row, 'id': unit, 'material_id': material, 'end_page': 6})); conn.commit()
    selected = call(api_client, case, 'get', '/api/plan/draft', params={'draftSubjectIds': case['subject']})
    assert selected.status_code == 200 and selected.json()['data']['summary']['totalPages'] == 20, selected.text
    if stale:
        conn.execute(update(t.materials).where(t.materials.c.id == case['material']).values(analysis_version='same-scope-new-version')); conn.commit()
    model({'ready': True, 'question': None, 'windows': []})
    resplit = prepare(api_client, case, request='다시 나눠줘')
    assert resplit.status_code == 200, resplit.text
    identifier = resplit.json()['data']['previewId']
    comparison = call(api_client, case, 'get', '/api/plan/resplit', params={'previewId': identifier})
    assert comparison.status_code == 200, comparison.text
    assert sum(day['after'] for day in comparison.json()['data']['days']) == 50
    approved = call(api_client, case, 'post', '/api/plan/approve', json={'previewId': identifier})
    assert approved.status_code == 200, approved.text
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id == other)) == 'draft'
    assert conn.scalar(select(t.subjects.c.status).where(t.subjects.c.id == case['subject'])) == 'active'
    assert set(conn.scalars(select(t.study_tasks.c.subject_id))) == {case['subject']}
    ranges = conn.execute(select(t.study_task_ranges)).mappings().all()
    assert all(row['unit_id'] == case['unit'] for row in ranges)
    assert sorted(page for row in ranges for page in range(row['start_page'], row['end_page']+1)) == list(range(1, 21))
