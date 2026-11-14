"""Manual contents keep blank input distinct from invalid characters and schema."""
import json

import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_material_storage import add_file, material_case


@pytest.fixture
def toc_input_case(material_case, domain_connection, monkeypatch):
    from app.ai.gateway import AiGateway

    def unexpected_ai(*args, **kwargs):
        raise AssertionError('Invalid contents must not invoke AI')

    monkeypatch.setattr(AiGateway, 'run', unexpected_ai)
    material, _, _ = add_file(domain_connection, material_case)
    return {**material_case, 'materialId': material}


def toc(client, case, text, **changes):
    body = {'subjectId': case['subject'], 'materialId': case['materialId'], 'text': text, **changes}
    return client.post('/api/materials/toc', headers={**case['headers'], 'Content-Type': 'application/json'},
                       content=json.dumps(body))


def test_whitespace_contents_returns_empty_and_preserves_all_material_state(api_client, toc_input_case, domain_connection):
    tables = (t.materials, t.units, t.concepts, t.ai_jobs, t.ai_saved_results, t.ai_usage_logs)
    before = [domain_connection.execute(select(table)).all() for table in tables]
    domain_connection.commit()
    for text in ('', ' ', '\t\r\n', '\u3000\u00a0'):
        response = toc(api_client, toc_input_case, text)
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'EMPTY'
        assert response.headers['cache-control'] == 'no-store'
    assert [domain_connection.execute(select(table)).all() for table in tables] == before


def test_malformed_contents_remain_invalid_input(api_client, toc_input_case):
    for value in (None, 123, False, [], {}, '\x00', ' \x00 ', '\ud800', '1. Intro 2~1쪽'):
        response = toc(api_client, toc_input_case, value)
        assert response.status_code == 422, response.text
        assert response.json()['error']['code'] == 'INVALID_INPUT'
    response = api_client.post('/api/materials/toc', headers=toc_input_case['headers'], json={
        'subjectId': toc_input_case['subject'], 'materialId': toc_input_case['materialId']})
    assert response.status_code == 422 and response.json()['error']['code'] == 'INVALID_INPUT'


def test_empty_contents_cannot_bypass_authentication_or_material_ownership(api_client, toc_input_case):
    case = toc_input_case
    foreign = {**case, 'headers': {'X-Anonymous-Id': case['other_user']}}
    response = toc(api_client, foreign, ' ')
    assert response.status_code == 404 and response.json()['error']['code'] == 'NO_MATERIAL'
    response = toc(api_client, case, ' ', subjectId=case['empty_subject'])
    assert response.status_code == 422 and response.json()['error']['code'] == 'INVALID_INPUT'
    response = toc(api_client, {**case, 'headers': {}}, ' ')
    assert response.status_code == 401 and response.json()['error']['code'] == 'NO_SESSION'
