"""Independent HTTP/DB checks for fixed initial-pass evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from uuid import uuid4

from sqlalchemy import select, update

from app.db import schema as t
from tests.test_initial_pass import fresh_unit, placement, record, passes
from tests.test_record_transactions import headers
from tests.test_focus_concurrency import fast_busy


def add_scope(conn, unit, template, start, end, *, included=True):
    conn.execute(t.concepts.insert().values(id=str(uuid4()), unit_id=unit, code=str(uuid4()), title='independent scope',
        start_page=start, end_page=end, in_scope=included, sort_order=start,
        created_at=template.row['created_at'], updated_at=template.row['created_at']))


def test_disjoint_scope_and_overlapping_evidence_remain_fixed_during_correction(api_client, demo_user, domain_connection):
    conn = domain_connection
    template, unit = fresh_unit(conn, demo_user)
    for start, end, included in [(12, 14, True), (14, 15, True), (16, 17, False), (18, 20, True)]:
        add_scope(conn, unit, template, start, end, included=included)
    first = placement(conn, template, 12, 14)
    second = placement(conn, template, 14, 15)
    third = placement(conn, template, 18, 20)
    replacement = placement(conn, template, 15, 15)
    conn.commit()
    record(api_client, demo_user, first)
    record(api_client, demo_user, third)
    assert passes(conn, unit) == []
    conn.commit()
    record(api_client, demo_user, second)
    original = passes(conn, unit)
    assert len(original) == 1
    assert original[0]['evidence_snapshot']['requiredRanges'] == [[12, 15], [18, 20]]
    ids = set(conn.scalars(select(t.study_records.c.id).where(t.study_records.c.task_id.in_([first, second, third]))))
    assert set(original[0]['evidence_snapshot']['recordIds']) == ids
    conn.commit()
    record(api_client, demo_user, second, 'none')
    invalid = passes(conn, unit)
    assert invalid[0]['invalidated_at'] is not None
    assert {**invalid[0], 'invalidated_at': None} == original[0]
    # A new complete page cannot replace the immutable evidence record set.
    conn.commit()
    record(api_client, demo_user, replacement)
    assert passes(conn, unit) == invalid
    # Removing the missing page from today's scope cannot rewrite past criteria.
    conn.execute(update(t.concepts).where(t.concepts.c.unit_id == unit, t.concepts.c.start_page == 14,
        t.concepts.c.retired_at.is_(None)).values(in_scope=False))
    conn.commit()
    record(api_client, demo_user, second, 'none')
    assert passes(conn, unit) == invalid
    conn.commit()
    record(api_client, demo_user, second)
    assert passes(conn, unit) == original


def test_same_page_numbers_in_another_pdf_cannot_fill_initial_pass(api_client, demo_user, domain_connection):
    conn = domain_connection
    template, unit = fresh_unit(conn, demo_user)
    source = dict(conn.execute(select(t.units).where(t.units.c.id == unit)).mappings().one())
    material = dict(conn.execute(select(t.materials).where(t.materials.c.id == source['material_id'])).mappings().one())
    second_material, second_unit = str(uuid4()), str(uuid4())
    conn.execute(t.materials.insert().values(**{**material, 'id': second_material, 'sha256': 'independent-pdf'}))
    conn.execute(t.units.insert().values(**{**source, 'id': second_unit, 'material_id': second_material}))
    first = placement(conn, template, 12, 13)
    other = placement(conn, template, 14, 20)
    conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == other).values(unit_id=second_unit))
    final = placement(conn, template, 14, 20)
    conn.commit()
    record(api_client, demo_user, first)
    record(api_client, demo_user, other)
    assert passes(conn, unit) == [] and passes(conn, second_unit) == []
    other_record = conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == other))
    conn.commit()
    record(api_client, demo_user, final)
    created = passes(conn, unit)
    assert len(created) == 1 and passes(conn, second_unit) == []
    assert created[0]['evidence_snapshot']['materialId'] == source['material_id']
    assert other_record not in created[0]['evidence_snapshot']['recordIds']


def test_concurrent_distinct_partial_tasks_create_exactly_one_initial_pass(api_client, demo_user, domain_connection, monkeypatch):
    from app.services import records
    conn = domain_connection
    template, unit = fresh_unit(conn, demo_user)
    first, second = placement(conn, template, 12, 16), placement(conn, template, 17, 20)
    conn.commit()
    fast_busy(conn)
    entered, release = Event(), Event()
    original = records.create_initial_passes
    def held(*args):
        original(*args)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(records, 'create_initial_passes', held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        ongoing = executor.submit(record, api_client, demo_user, first)
        try:
            assert entered.wait(5)
            conflict = api_client.post('/api/records', headers=headers(demo_user), json={'taskId': second, 'result': 'done'})
            assert conflict.status_code == 409, conflict.text
        finally:
            release.set()
        assert ongoing.result(timeout=5).status_code == 200
    monkeypatch.setattr(records, 'create_initial_passes', original)
    assert passes(conn, unit) == []
    conn.commit()
    record(api_client, demo_user, second)
    created = passes(conn, unit)
    assert len(created) == 1 and len(created[0]['evidence_snapshot']['recordIds']) == 2
    conn.commit()
    record(api_client, demo_user, first)
    record(api_client, demo_user, second)
    assert passes(conn, unit) == created
