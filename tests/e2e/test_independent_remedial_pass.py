"""Independent B-02 obligation identity and reserved-evidence checks."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.db import schema as t
from tests.test_initial_pass import placement, record
from tests.test_remedial_pass import obligation, remedials
from tests.test_record_transactions import task_for, headers


def test_unlinked_same_unit_completion_cannot_fill_obligation_hole(api_client, demo_user, domain_connection):
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]['unit_id']
    linked = placement(conn, template, 12, 20, task_type='remedial')
    unrelated = placement(conn, template, 16, 20, task_type='remedial')
    oid = obligation(conn, demo_user, template, [linked])
    conn.commit()
    record(api_client, demo_user, linked, result='partial', toPage=15)
    record(api_client, demo_user, unrelated)
    assert remedials(conn, uid) == []
    conn.commit()
    changed = api_client.patch(f'/api/tasks/{linked}/record', headers=headers(demo_user), json={'toPage': 20})
    assert changed.status_code == 200, changed.text
    found = remedials(conn, uid)
    assert len(found) == 1 and found[0]['evidence_key'] == oid
    unrelated_record = conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == unrelated))
    assert unrelated_record not in found[0]['evidence_snapshot']['recordIds']


def test_overlapping_split_obligation_counts_union_and_never_creates_two_passes(api_client, demo_user, domain_connection):
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]['unit_id']
    first = placement(conn, template, 12, 16, task_type='remedial')
    second = placement(conn, template, 16, 20, task_type='remedial')
    oid = obligation(conn, demo_user, template, [first, second])
    conn.commit()
    record(api_client, demo_user, first)
    assert remedials(conn, uid) == []
    conn.commit()
    record(api_client, demo_user, second)
    before = remedials(conn, uid)
    assert len(before) == 1 and before[0]['evidence_key'] == oid
    assert before[0]['evidence_snapshot']['requiredRanges'] == [[12, 20]]
    conn.commit()
    record(api_client, demo_user, first)
    record(api_client, demo_user, second)
    assert remedials(conn, uid) == before


def test_invalidated_pass_book_cannot_be_reassigned_and_original_proof_restores(api_client, demo_user, domain_connection):
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]['unit_id']
    task = placement(conn, template, 12, 20, task_type='remedial')
    original_obligation = obligation(conn, demo_user, template, [task])
    conn.commit()
    record(api_client, demo_user, task)
    before = remedials(conn, uid)
    assert len(before) == 1
    conn.commit()
    response = api_client.patch(f'/api/tasks/{task}/record', headers=headers(demo_user), json={'toPage': 19})
    assert response.status_code == 200, response.text
    assert remedials(conn, uid)[0]['invalidated_at'] is not None
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            obligation(conn, demo_user, template, [task])
    conn.commit()
    response = api_client.patch(f'/api/tasks/{task}/record', headers=headers(demo_user), json={'toPage': 20})
    assert response.status_code == 200, response.text
    assert remedials(conn, uid) == before
    assert before[0]['evidence_key'] == original_obligation
