"""Independent B-02 first-pass qualification rejects unrelated record evidence."""
import pytest
from sqlalchemy import update
from app.db import schema as t
from tests.test_initial_pass import placement, record
from tests.test_planless_review_correction import direct_record
from tests.test_remedial_pass import obligation, remedials
from tests.test_record_transactions import task_for


@pytest.mark.parametrize('invalid', ['other_unit', 'other_owner'])
def test_full_first_requires_this_units_owned_records_before_remedial_pass(api_client, demo_user, domain_connection, invalid):
    conn = domain_connection
    template, direct, own_record, full = direct_record(conn, demo_user)
    uid = template.ranges[0]['unit_id']
    conn.execute(t.learning_passes.delete().where(t.learning_passes.c.unit_id == uid, t.learning_passes.c.completion_kind == 'initial'))
    conn.commit()
    source_user = demo_user
    if invalid == 'other_owner':
        source_user = api_client.post('/api/session').json()['data']['anonymousId']
    wrong = task_for(conn, source_user, 'DB1' if invalid == 'other_unit' else 'DB2')
    forged = {'schemaVersion': '1', 'materialId': template.ranges[0]['material_id'],
        'recordIds': [wrong.record['id']], 'requiredRanges': [[12, 20]], 'directRequestHash': 'b' * 64}
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == full).values(
        evidence_key='direct:' + 'a' * 64, evidence_snapshot=forged))
    remedial = placement(conn, template, 12, 20, task_type='remedial')
    obligation(conn, demo_user, template, [remedial])
    conn.commit()
    record(api_client, demo_user, remedial)
    assert remedials(conn, uid) == []
    # Restoring the original owned proof permits this already-completed obligation once.
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == full).values(
        evidence_snapshot={**forged, 'recordIds': [own_record]}))
    conn.commit()
    record(api_client, demo_user, remedial)
    assert len(remedials(conn, uid)) == 1
