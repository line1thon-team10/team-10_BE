"""All approval transports share the missing-preview contract; other errors do not."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.clock import now_utc
from app.db import schema as t

ID = '11111111-1111-4111-8111-111111111111'
BODY_APPROVALS = [
    ('POST', '/api/plan/approve', {}),
    ('POST', '/api/plan/resplit/apply', {}),
    ('POST', '/api/plan/replan', {'key': 'A'}),
    ('POST', '/api/plan/additions', {}),
    ('POST', '/api/plan/today', {'action': 'rest'}),
    ('POST', '/api/plan/empty-time', {'option': 'rest'}),
    ('POST', '/api/schedules/voice/apply', {}),
    ('PATCH', f'/api/subjects/{ID}', {}),
    ('PATCH', f'/api/schedules/{ID}@2026-11-16', {}),
    ('PUT', '/api/units', {}),
    ('PATCH', f'/api/materials/{ID}/units', {}),
]
QUERY_APPROVALS = [f'/api/subjects/{ID}', f'/api/schedules/{ID}@2026-11-16', '/api/plan/resplit']


@pytest.fixture
def approval_headers(api_client, domain_connection):
    identifier, now = str(uuid4()), now_utc()
    domain_connection.execute(t.users.insert().values(id=identifier, created_at=now, updated_at=now))
    domain_connection.commit()
    return {'X-Anonymous-Id': identifier}


def assert_error(response, status, code):
    assert response.status_code == status, (response.request.method, response.request.url, response.text)
    assert response.json()['error']['code'] == code
    assert response.headers['cache-control'] == 'no-store'


def test_missing_preview_in_every_approval_body_including_absent_body(api_client, approval_headers, domain_connection):
    for method, path, other_fields in BODY_APPROVALS:
        for kwargs in ({}, {'json': {}}, {'json': other_fields}, {'content': 'null', 'headers': {**approval_headers, 'Content-Type': 'application/json'}}):
            response = api_client.request(method, path, **({'headers': approval_headers} | kwargs))
            assert_error(response, 409, 'PREVIEW_REQUIRED')
    assert domain_connection.execute(select(t.plan_proposals)).all() == []
    assert domain_connection.execute(select(t.plans)).all() == []


def test_missing_delete_query_preview_is_required(api_client, approval_headers):
    for path in QUERY_APPROVALS:
        assert_error(api_client.delete(path, headers=approval_headers), 409, 'PREVIEW_REQUIRED')


def test_explicit_invalid_preview_ids_remain_invalid_input(api_client, approval_headers):
    for method, path, other_fields in BODY_APPROVALS:
        for identifier in (None, '', 'not-a-uuid', 123):
            response = api_client.request(method, path, headers=approval_headers, json={**other_fields, 'previewId': identifier})
            assert_error(response, 422, 'INVALID_INPUT')
    for path in QUERY_APPROVALS:
        for identifier in ('', 'not-a-uuid', 'null'):
            assert_error(api_client.delete(path, headers=approval_headers, params={'previewId': identifier}), 422, 'INVALID_INPUT')


def test_other_missing_or_invalid_fields_do_not_become_preview_errors(api_client, approval_headers):
    for path in ('/api/plan/replan', '/api/plan/today', '/api/plan/empty-time'):
        assert_error(api_client.post(path, headers=approval_headers, json={'previewId': ID}), 422, 'INVALID_INPUT')
    for method, path, body in [
        ('PUT', '/api/units', {'units': []}),
        ('PUT', '/api/units', {'subjectId': ID, 'materialId': ID}),
        ('PATCH', f'/api/materials/{ID}/units', {'inExam': True}),
        ('POST', '/api/plan/today', {'action': 'invalid'}),
        ('POST', '/api/plan/empty-time', {'option': 'invalid'}),
        ('PATCH', '/api/subjects/not-a-uuid', {}),
        ('POST', '/api/subjects', {}),
    ]:
        assert_error(api_client.request(method, path, headers=approval_headers, json=body), 422, 'INVALID_INPUT')


def test_invalid_json_or_nonobject_body_remains_invalid_input(api_client, approval_headers):
    for method, path, _ in BODY_APPROVALS:
        for content in ('{', '[]', '"text"', '123'):
            response = api_client.request(method, path, headers={**approval_headers, 'Content-Type': 'application/json'}, content=content)
            assert_error(response, 422, 'INVALID_INPUT')


def test_shared_edit_routes_still_accept_commands_without_preview_id(api_client, approval_headers):
    # Well-formed edit commands reach owned-material lookup, not approval validation.
    response = api_client.put('/api/units', headers=approval_headers,
        json={'subjectId': ID, 'materialId': ID, 'units': []})
    assert_error(response, 404, 'NO_MATERIAL')
    response = api_client.patch(f'/api/materials/{ID}/units', headers=approval_headers, json={'unitId': ID, 'inExam': True})
    assert_error(response, 404, 'NO_MATERIAL')


def test_authentication_still_precedes_missing_preview(api_client):
    assert_error(api_client.post('/api/plan/approve', json={}), 401, 'NO_SESSION')


def test_preview_query_remains_optional_on_compare_and_required_on_cancel(api_client, approval_headers):
    response = api_client.get('/api/plan/resplit', headers=approval_headers)
    assert response.json().get('error', {}).get('code') != 'PREVIEW_REQUIRED', response.text
