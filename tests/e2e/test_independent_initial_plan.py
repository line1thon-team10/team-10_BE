"""Independent initial-plan HTTP races and transaction boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from app.db import schema as t
from tests.test_plan_draft import NOW, TODAY, plan_case


def get_draft(client, case, **params):
    result = client.get('/api/plan/draft', params=params, headers={'X-Anonymous-Id': case['user']})
    return result


def approve(client, case, preview):
    return client.post('/api/plan/approve', headers={'X-Anonymous-Id': case['user']}, json={'previewId': preview})


def unchanged_domain(conn):
    return {table.name: [dict(row) for row in conn.execute(select(table).order_by(table.c.id)).mappings()]
            for table in (t.users, t.subjects, t.materials, t.units, t.concepts, t.plans,
                          t.study_blocks, t.study_tasks, t.study_task_ranges)}


def test_multi_material_concept_union_and_readonly_get_then_exact_approval(api_client, domain_connection, plan_case):
    conn, case = domain_connection, plan_case
    for order, (start, end) in enumerate([(1, 10), (5, 15)], start=1):
        identifier = str(uuid4())
        conn.execute(t.concepts.insert().values(id=identifier, unit_id=case['unit'], code=identifier,
            title='Overlapping concept', sort_order=order, start_page=start, end_page=end,
            created_at=NOW, updated_at=NOW))
    material = dict(conn.execute(select(t.materials).where(t.materials.c.id == case['material'])).mappings().one())
    unit = dict(conn.execute(select(t.units).where(t.units.c.id == case['unit'])).mappings().one())
    material_id, unit_id = str(uuid4()), str(uuid4())
    conn.execute(t.materials.insert().values(**{**material, 'id': material_id, 'sha256': 'other-file'}))
    conn.execute(t.units.insert().values(**{**unit, 'id': unit_id, 'material_id': material_id, 'end_page': 6}))
    before = unchanged_domain(conn)
    conn.commit()
    response = get_draft(api_client, case)
    assert response.status_code == 200, response.text
    candidate = response.json()['data']
    assert candidate['summary']['totalPages'] == 21
    assert unchanged_domain(conn) == before
    conn.commit()
    assert get_draft(api_client, case).json() == response.json()
    result = approve(api_client, case, candidate['previewId'])
    assert result.status_code == 200, result.text
    rows = conn.execute(select(t.study_task_ranges, t.units.c.material_id).join(t.units,
        t.study_task_ranges.c.unit_id == t.units.c.id)).mappings().all()
    actual = [(row['material_id'], page) for row in rows for page in range(row['start_page'], row['end_page'] + 1)]
    expected = [(case['material'], page) for page in range(1, 16)] + [(material_id, page) for page in range(1, 7)]
    assert sorted(actual) == sorted(expected) and len(actual) == len(set(actual))
    assert all(row['scheduled_date'] < TODAY + timedelta(days=3) for row in conn.execute(select(t.study_tasks)).mappings())


def test_input_change_during_unlocked_allocation_rejects_stale_candidate(api_client, domain_connection, plan_case, monkeypatch):
    from app.db.session import engine
    from app.services import plan_draft
    real_allocate = plan_draft.allocate
    changed = []

    def mutate_after_calculation(request):
        result = real_allocate(request)
        with engine.begin() as independent:
            independent.execute(update(t.subjects).where(t.subjects.c.id == plan_case['subject']).values(target_score=80))
        changed.append(True)
        return result

    monkeypatch.setattr(plan_draft, 'allocate', mutate_after_calculation)
    result = get_draft(api_client, plan_case)
    assert changed == [True]
    assert result.status_code == 409 and result.json()['error']['code'] == 'PREVIEW_STALE', result.text
    for table in (t.plan_proposals, t.plans, t.study_blocks, t.study_tasks, t.study_task_ranges):
        assert domain_connection.execute(select(table)).first() is None


def test_approval_failure_rolls_back_header_subjects_ranges_and_acceptance(api_client, domain_connection, plan_case):
    from app.db.session import engine
    case, conn = plan_case, domain_connection
    preview = get_draft(api_client, case).json()['data']['previewId']
    before = unchanged_domain(conn)
    conn.commit()

    def fail_after_range(_conn, _cursor, _statement, _parameters, context, _many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'study_task_ranges':
            raise RuntimeError('Independent failure after placement ranges were written')

    event.listen(engine, 'after_cursor_execute', fail_after_range)
    try:
        failed = approve(api_client, case, preview)
    finally:
        event.remove(engine, 'after_cursor_execute', fail_after_range)
    assert failed.status_code == 500, failed.text
    assert unchanged_domain(conn) == before
    conn.commit()
    assert approve(api_client, case, preview).status_code == 200
    after = unchanged_domain(conn)
    conn.commit()
    assert approve(api_client, case, preview).status_code == 200
    assert unchanged_domain(conn) == after


@pytest.mark.parametrize('same_candidate', [True, False])
def test_two_http_approvals_cannot_duplicate_plan_or_ranges(api_client, domain_connection, plan_case, same_candidate):
    from app.db.session import engine
    conn, case = domain_connection, plan_case
    preview = get_draft(api_client, case).json()['data']['previewId']
    second_preview = preview if same_candidate else get_draft(api_client, case,
        draftSubjectIds=case['subject']).json()['data']['previewId']
    if not same_candidate:
        assert second_preview != preview
    arrived, release, mutex = Event(), Event(), Lock()
    observed = []

    def small_sqlite_wait(dbapi, _record, _proxy):
        if conn.dialect.name == 'sqlite':
            dbapi.execute('PRAGMA busy_timeout=50')

    def hold_first_range(_conn, _cursor, _statement, _parameters, context, _many):
        if not context.isinsert or getattr(context.compiled.statement.table, 'name', None) != 'study_task_ranges':
            return
        with mutex:
            if observed:
                return
            observed.append(True)
        arrived.set()
        assert release.wait(8), 'Independent test never released first approval'

    event.listen(engine, 'checkout', small_sqlite_wait)
    event.listen(engine, 'after_cursor_execute', hold_first_range)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(approve, api_client, case, preview)
            try:
                assert arrived.wait(8), 'First approval did not reach range insertion'
                second = pool.submit(approve, api_client, case, second_preview).result(timeout=5)
                assert second.status_code == 409, second.text
            finally:
                release.set()
            assert first.result(timeout=5).status_code == 200
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', hold_first_range)
        event.remove(engine, 'checkout', small_sqlite_wait)
    plans = conn.execute(select(t.plans)).mappings().all()
    assert len(plans) == 1 and plans[0]['revision'] == 1
    ranges = conn.execute(select(t.study_task_ranges)).mappings().all()
    assert sorted(page for row in ranges for page in range(row['start_page'], row['end_page'] + 1)) == list(range(1, 21))
    snapshot = unchanged_domain(conn)
    conn.commit()
    retried = approve(api_client, case, second_preview)
    assert retried.status_code == (200 if same_candidate else 409), retried.text
    if not same_candidate:
        assert retried.json()['error']['code'] == 'PREVIEW_STALE'
    assert unchanged_domain(conn) == snapshot
