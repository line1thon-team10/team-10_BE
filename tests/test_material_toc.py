import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_material_storage import material_case, pdf_bytes, upload


@pytest.mark.parametrize('text', ['1. Intro 1~1쪽', '1) Intro p1-1', ' \n1. Intro 1–1쪽\n '])
def test_explicit_toc_is_parsed_without_saving(api_client, material_case, domain_connection, text):
    case, conn = material_case, domain_connection
    accepted = upload(api_client, case, pdf_bytes()).json()['data']
    before = (conn.execute(select(t.materials)).all(), conn.execute(select(t.ai_jobs)).all())
    conn.commit()
    result = api_client.post('/api/materials/toc', headers=case['headers'], json={
        'subjectId': case['subject'], 'materialId': accepted['materialId'], 'text': text})
    assert result.status_code == 200, result.text
    assert result.json()['data'] == {'units': [{'no': 1, 'title': 'Intro', 'from': 1, 'to': 1}]}
    assert conn.execute(select(t.units)).first() is None
    assert conn.execute(select(t.concepts)).first() is None
    assert (conn.execute(select(t.materials)).all(), conn.execute(select(t.ai_jobs)).all()) == before


@pytest.mark.parametrize('text', ['', '  ', '0. Intro 1~1쪽', '1. Intro 0~1쪽', '1. Intro 1~2쪽',
                                  '1. Intro 1~1쪽\n1. Duplicate 1~1쪽', '1. Intro 1~1쪽\n2. Overlap 1~1쪽'])
def test_invalid_toc_ranges_and_empty_text_are_rejected(api_client, material_case, text):
    case = material_case
    accepted = upload(api_client, case, pdf_bytes()).json()['data']
    result = api_client.post('/api/materials/toc', headers=case['headers'], json={
        'subjectId': case['subject'], 'materialId': accepted['materialId'], 'text': text})
    assert result.status_code == 422, result.text


def test_toc_ownership_and_subject_match(api_client, material_case):
    case = material_case
    accepted = upload(api_client, case, pdf_bytes()).json()['data']
    body = {'subjectId': case['subject'], 'materialId': accepted['materialId'], 'text': '1. Intro 1~1쪽'}
    assert api_client.post('/api/materials/toc', headers={'X-Anonymous-Id': case['other_user']}, json=body).status_code == 404
    body['subjectId'] = case['empty_subject']
    assert api_client.post('/api/materials/toc', headers=case['headers'], json=body).status_code == 422


def test_natural_toc_uses_owned_gateway_and_never_persists_structure(
        api_client, material_case, domain_connection, monkeypatch):
    from app.ai.gateway import GatewayResult
    from app.services import material_toc
    case, conn = material_case, domain_connection
    accepted = upload(api_client, case, pdf_bytes()).json()['data']
    calls = []
    def run(self, request, *, validator, provider):
        calls.append(request)
        return GatewayResult({'units': [{'no': 1, 'title': 'Intro', 'from': 1, 'to': 1}]}, '', 'live', 1)
    monkeypatch.setattr(material_toc.AiGateway, 'run', run)
    response = api_client.post('/api/materials/toc', headers=case['headers'], json={
        'subjectId': case['subject'], 'materialId': accepted['materialId'], 'text': '첫 단원 Intro는 첫 쪽입니다'})
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and calls[0].owner_id == case['user']
    assert calls[0].identity.materials[0].material_id == accepted['materialId']
    assert conn.execute(select(t.units)).first() is None
