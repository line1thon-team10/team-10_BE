"""Malformed candidate query IDs must stop before proposal lookup."""
from uuid import uuid4

from app.core.clock import now_utc
from app.db import schema as t


def test_resplit_query_uuid_is_validated_before_candidate_lookup(api_client, domain_connection):
    user_id, now = str(uuid4()), now_utc()
    domain_connection.execute(t.users.insert().values(id=user_id, created_at=now, updated_at=now))
    domain_connection.commit()
    headers = {'X-Anonymous-Id': user_id}
    for method in ('GET', 'DELETE'):
        for value in ('', 'not-a-uuid', 'null', '123'):
            response = api_client.request(method, '/api/plan/resplit', headers=headers, params={'previewId': value})
            assert response.status_code == 422, response.text
            assert response.json()['error']['code'] == 'INVALID_INPUT'
        response = api_client.request(method, '/api/plan/resplit', headers=headers, params={'previewId': str(uuid4())})
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'NO_PREVIEW'
