"""Durable HTTP response replay across later edits, rollback and competing requests."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.db import schema as t


def task(client, user):
    response = client.get('/api/today', headers={'X-Anonymous-Id':user})
    assert response.status_code == 200
    return next(row for row in response.json()['data']['tasks'] if row['pages'] > 1)


def request_headers(user, key=None):
    return {'X-Anonymous-Id':user, **({'Idempotency-Key': key} if key else {})}


def test_replay_keeps_first_response_after_later_correction(api_client, demo_user, domain_connection):
    conn, row, key = domain_connection, task(api_client, demo_user), str(uuid4())
    body = {'taskId':row['id'], 'result':'done'}
    response = api_client.post('/api/records', headers=request_headers(demo_user,key), json=body)
    assert response.status_code == 200, response.text
    corrected = api_client.patch(f"/api/tasks/{row['id']}/record", headers=request_headers(demo_user,key),
                                 json={'completedRanges':[]})
    assert corrected.status_code == 200, corrected.text
    before = conn.execute(select(t.study_records)).all()
    conn.commit()
    repeated = api_client.post('/api/records', headers=request_headers(demo_user,key), json=body)
    assert repeated.status_code == 200 and repeated.json() == response.json()
    assert conn.execute(select(t.study_records)).all() == before
    assert len(conn.execute(select(t.record_requests)).all()) == 2
    conn.commit()
    conflict = api_client.post('/api/records', headers=request_headers(demo_user,key),
                              json={**body,'result':'none'})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    again = api_client.patch(f"/api/tasks/{row['id']}/record", headers=request_headers(demo_user,key),
                             json={'completedRanges':[]})
    assert again.json() == corrected.json()


@pytest.mark.parametrize('key', ['', 'bad', 'null'])
def test_invalid_key_does_not_write(api_client, demo_user, domain_connection, key):
    row = task(api_client,demo_user)
    response = api_client.post('/api/records', headers={**request_headers(demo_user),'Idempotency-Key':key},
                              json={'taskId':row['id'],'result':'done'})
    assert response.status_code == 422
    assert domain_connection.execute(select(t.record_requests)).first() is None


def test_failure_rolls_back_record_and_response_and_same_key_can_retry(api_client, demo_user, domain_connection):
    from app.db.session import engine
    conn, row, key = domain_connection, task(api_client,demo_user), str(uuid4())
    before = conn.execute(select(t.study_records)).all()
    conn.commit()
    def fail_save(connection, cursor, statement, parameters, context, many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'record_requests':
            raise RuntimeError('Fail just before saving successful response')
    event.listen(engine,'before_cursor_execute',fail_save)
    try:
        failed = api_client.post('/api/records', headers=request_headers(demo_user,key),
                                 json={'taskId':row['id'],'result':'done'})
    finally:
        event.remove(engine,'before_cursor_execute',fail_save)
    assert failed.status_code == 500
    assert conn.execute(select(t.study_records)).all() == before
    assert conn.execute(select(t.record_requests)).first() is None
    conn.commit()
    retried = api_client.post('/api/records', headers=request_headers(demo_user,key),
                             json={'taskId':row['id'],'result':'done'})
    assert retried.status_code == 200


def test_same_key_is_scoped_by_owner_and_reset_discards_old_record_replays(api_client,demo_user,domain_connection):
    conn, key = domain_connection, str(uuid4())
    other = api_client.post('/api/session').json()['data']['anonymousId']
    for user in (demo_user, other):
        row = task(api_client,user)
        response = api_client.post('/api/records', headers=request_headers(user,key),
                                  json={'taskId':row['id'],'result':'done'})
        assert response.status_code == 200
    assert len(conn.execute(select(t.record_requests)).all()) == 2
    conn.commit()
    assert api_client.post('/api/demo/reset', headers=request_headers(demo_user)).status_code == 200
    remaining = conn.execute(select(t.record_requests)).mappings().one()
    assert remaining['user_id'] == other


def test_competing_requests_cannot_duplicate_success(api_client,demo_user,domain_connection):
    from app.db.session import engine
    row, key, arrived, release = task(api_client,demo_user), str(uuid4()), Event(), Event()
    def short_busy(dbapi_conn, record, proxy):
        if domain_connection.dialect.name == 'sqlite':
            dbapi_conn.execute('PRAGMA busy_timeout=50')
    def pause_after_save(conn, cursor, statement, parameters, context, many):
        if context.isinsert and getattr(context.compiled.statement.table,'name',None) == 'record_requests':
            arrived.set()
            assert release.wait(5)
    event.listen(engine,'checkout',short_busy)
    event.listen(engine,'after_cursor_execute',pause_after_save)
    def send():
        return api_client.post('/api/records',headers=request_headers(demo_user,key),
                               json={'taskId':row['id'],'result':'done'})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(send)
            try:
                assert arrived.wait(5)
                second = pool.submit(send).result(timeout=3)
                assert second.status_code == 409
                assert second.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
            finally:
                release.set()
            result = first.result(timeout=5)
            assert result.status_code == 200
    finally:
        release.set()
        event.remove(engine,'checkout',short_busy)
        event.remove(engine,'after_cursor_execute',pause_after_save)
    assert send().json() == result.json()
    assert len(domain_connection.execute(select(t.record_requests)).all()) == 1
