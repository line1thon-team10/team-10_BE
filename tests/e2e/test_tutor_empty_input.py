"""Blank question is a semantic input error, not a malformed JSON/schema error."""
import json

from sqlalchemy import select

from app.db import schema as t
from tests.test_material_storage import material_case
from tests.test_tutor_e2e import tutor_case


def post_question(client, case, body):
    return client.post('/api/tutor', headers={**case['headers'], 'Content-Type': 'application/json'},
                       content=json.dumps(body))


def test_whitespace_question_returns_empty_without_ai_or_domain_writes(api_client, tutor_case, domain_connection, monkeypatch):
    from app.ai.gateway import AiGateway

    def unexpected_ai(*args, **kwargs):
        raise AssertionError('Blank questions must not invoke AI')

    monkeypatch.setattr(AiGateway, 'run', unexpected_ai)
    tables = (t.tutor_messages, t.tutor_sources, t.ai_jobs, t.ai_saved_results, t.ai_usage_logs)
    before = [domain_connection.execute(select(table)).all() for table in tables]
    domain_connection.commit()
    for question in ('', ' ', '\t\r\n', '\u3000\u00a0'):
        response = post_question(api_client, tutor_case, {'question': question})
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'EMPTY'
        assert response.headers['cache-control'] == 'no-store'
    assert [domain_connection.execute(select(table)).all() for table in tables] == before


def test_invalid_question_types_and_encoded_characters_remain_422(api_client, tutor_case):
    bodies = [{}, *({'question': value} for value in (None, 123, False, [], {}, '\x00', ' \x00 ', '\ud800'))]
    for body in bodies:
        response = post_question(api_client, tutor_case, body)
        assert response.status_code == 422, response.text
        assert response.json()['error']['code'] == 'INVALID_INPUT'


def test_nonempty_question_still_trims_and_uses_actual_answer_pipeline(api_client, tutor_case):
    response = post_question(api_client, tutor_case, {'question': '\t Explain AVL rotations \u3000'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['question'] == 'Explain AVL rotations'
    assert response.json()['data']['pages'] == [{'materialId': tutor_case['materialId'], 'from': 1, 'to': 1}]
