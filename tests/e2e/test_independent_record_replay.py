"""Independent record replay normalization, date and endpoint isolation."""
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.db import schema as t
from tests.e2e.test_record_replay import request_headers, task


def persisted(conn):
    return {table.name: conn.execute(select(table).order_by(table.c.id)).all()
            for table in (t.study_records, t.study_record_ranges, t.learning_passes, t.user_notices)}


def test_first_record_replays_after_date_changes_without_rewriting_evidence(api_client, demo_user, domain_connection):
    conn, row, key = domain_connection, task(api_client, demo_user), str(uuid4())
    body = {'taskId': row['id'], 'result': 'done'}
    first = api_client.post('/api/records', headers=request_headers(demo_user, key), json=body)
    assert first.status_code == 200, first.text
    current_date = conn.scalar(select(t.users.c.demo_date).where(t.users.c.id == demo_user))
    conn.execute(update(t.users).where(t.users.c.id == demo_user).values(demo_date=current_date + timedelta(days=1)))
    before = persisted(conn)
    conn.commit()
    again = api_client.post('/api/records', headers=request_headers(demo_user, key), json=body)
    assert again.status_code == 200 and again.json() == first.json()
    assert persisted(conn) == before
    assert len(conn.execute(select(t.record_requests)).all()) == 1


def test_uuid_spelling_and_json_field_order_replay_same_request(api_client, demo_user, domain_connection):
    row, key = task(api_client, demo_user), uuid4()
    first = api_client.post('/api/records', headers=request_headers(demo_user, str(key)),
        json={'taskId': row['id'], 'result': 'done'})
    assert first.status_code == 200, first.text
    before = persisted(domain_connection)
    domain_connection.commit()
    replay = api_client.post('/api/records', headers=request_headers(demo_user, key.hex.upper()),
        json={'result': 'done', 'taskId': UUID(row['id']).hex.upper()})
    assert replay.status_code == 200 and replay.json() == first.json(), replay.text
    assert persisted(domain_connection) == before
    assert len(domain_connection.execute(select(t.record_requests)).all()) == 1


def test_same_patch_key_on_two_tasks_has_separate_durable_responses(api_client, demo_user, domain_connection):
    conn, key = domain_connection, str(uuid4())
    headers = request_headers(demo_user)
    rows = api_client.get('/api/today', headers=headers).json()['data']['tasks']
    selected = [row for row in rows if row['pages'] > 1][:2]
    assert len(selected) == 2
    for row in selected:
        result = api_client.post('/api/records', headers=headers, json={'taskId': row['id'], 'result': 'done'})
        assert result.status_code == 200, result.text
    responses = []
    for row in selected:
        result = api_client.patch(f"/api/tasks/{row['id']}/record", headers=request_headers(demo_user, key),
            json={'completedRanges': []})
        assert result.status_code == 200, result.text
        responses.append(result)
    before = persisted(conn)
    conn.commit()
    for row, first in zip(selected, responses):
        result = api_client.patch(f"/api/tasks/{row['id']}/record", headers=request_headers(demo_user, key),
            json={'completedRanges': []})
        assert result.json() == first.json() and result.json()['data']['taskId'] == row['id']
    assert persisted(conn) == before
    assert len(conn.execute(select(t.record_requests)).all()) == 2


def test_duplicate_headers_rejected_before_record_or_replay_write(api_client, demo_user, domain_connection):
    conn, row, key = domain_connection, task(api_client, demo_user), str(uuid4())
    before = persisted(conn)
    conn.commit()
    headers = [('X-Anonymous-Id', demo_user), ('Idempotency-Key', key), ('Idempotency-Key', key)]
    response = api_client.post('/api/records', headers=headers, json={'taskId': row['id'], 'result': 'done'})
    assert response.status_code == 422, response.text
    assert persisted(conn) == before
    assert conn.execute(select(t.record_requests)).first() is None


def test_failed_business_validation_does_not_consume_idempotency_key(api_client, demo_user, domain_connection):
    conn, row, key = domain_connection, task(api_client, demo_user), str(uuid4())
    invalid = {'taskId': row['id'], 'result': 'partial', 'toPage': 999999}
    response = api_client.post('/api/records', headers=request_headers(demo_user, key), json=invalid)
    assert response.status_code in (400, 422), response.text
    assert conn.execute(select(t.record_requests)).first() is None
    conn.commit()
    corrected = api_client.post('/api/records', headers=request_headers(demo_user, key),
        json={'taskId': row['id'], 'result': 'done'})
    assert corrected.status_code == 200, corrected.text
    assert len(conn.execute(select(t.record_requests)).all()) == 1
