"""Evidence reserved by a different pass cannot be counted for initial again."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_initial_pass import fresh_unit, placement, record, passes


@pytest.mark.parametrize('invalidated', [False, True])
def test_new_initial_requires_fresh_full_evidence_after_another_pass(api_client, demo_user, domain_connection, invalidated):
    conn = domain_connection
    template, unit = fresh_unit(conn, demo_user)
    reserved_task = placement(conn, template, 12, 16)
    remainder = placement(conn, template, 17, 20)
    fresh_prefix = placement(conn, template, 12, 16)
    conn.commit()
    record(api_client, demo_user, reserved_task)
    reserved_record = conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == reserved_task))
    material = conn.scalar(select(t.units.c.material_id).where(t.units.c.id == unit))
    conn.execute(t.learning_passes.insert().values(id=str(uuid4()), unit_id=unit, pass_number=1,
        completion_kind='full_relearn', source_task_id=reserved_task, evidence_key=str(uuid4()),
        evidence_snapshot={'schemaVersion': '1', 'materialId': material, 'recordIds': [reserved_record],
            'sessionIds': [], 'requiredRanges': [[12, 16]]}, completed_at=template.row['created_at'],
        completed_date=template.row['scheduled_date'],
        invalidated_at=template.row['created_at'] if invalidated else None))
    original = passes(conn, unit)[0]
    conn.commit()
    record(api_client, demo_user, remainder)
    assert passes(conn, unit) == [original]
    conn.commit()
    record(api_client, demo_user, fresh_prefix)
    found = passes(conn, unit)
    assert len(found) == 2 and found[0] == original
    initial = found[1]
    assert initial['completion_kind'] == 'initial' and initial['pass_number'] == 2
    expected = set(conn.scalars(select(t.study_records.c.id).where(t.study_records.c.task_id.in_([remainder, fresh_prefix]))))
    assert set(initial['evidence_snapshot']['recordIds']) == expected
    assert reserved_record not in expected
