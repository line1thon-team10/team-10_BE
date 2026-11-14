"""Independent explicit/natural TOC HTTP flow; provider transport is the only double."""
import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_material_storage import material_case, upload
from tests.e2e.test_material_analysis_contract import two_page_pdf


def snapshot(conn):
    return {table.name: conn.execute(select(table).order_by(table.c.id)).all()
            for table in (t.materials, t.ai_jobs, t.units, t.concepts)}


def request(client, case, material, text):
    return client.post('/api/materials/toc', headers=case['headers'], json={
        'subjectId': case['subject'], 'materialId': material, 'text': text})


@pytest.mark.parametrize('text,valid', [
    ('1. Intro 1~1쪽\n2. Details 2~2쪽', True),
    ('1. Intro 1~2쪽\n2. Overlap 2~2쪽', False),
    ('1. Intro 2~1쪽', False), ('1. Intro 0~2쪽', False),
    ('1. Intro 1~3쪽', False), ('1. Intro 1~1쪽\n1. Duplicate 2~2쪽', False),
])
def test_explicit_toc_never_saves_or_calls_provider(api_client, material_case, domain_connection, text, valid):
    case, conn = material_case, domain_connection
    material = upload(api_client, case, two_page_pdf()).json()['data']['materialId']
    before = snapshot(conn)
    conn.commit()
    response = request(api_client, case, material, text)
    assert response.status_code == (200 if valid else 422), response.text
    if valid:
        assert response.json()['data'] == {'units': [
            {'no': 1, 'title': 'Intro', 'from': 1, 'to': 1}, {'no': 2, 'title': 'Details', 'from': 2, 'to': 2}]}
    assert snapshot(conn) == before
    assert conn.execute(select(t.ai_usage_logs)).first() is None
    assert conn.execute(select(t.ai_saved_results)).first() is None


@pytest.mark.parametrize('mode', ['good', 'failed', 'ambiguous', 'outside'])
def test_natural_toc_gateway_validates_and_preserves_domain(api_client, material_case, domain_connection, monkeypatch, mode):
    from app.ai.gateway import AiGateway, FunctionRef, ProviderSpec
    from app.services import material_toc
    original = AiGateway.run

    def observed(self, gateway_request, *, validator, provider=None, **kwargs):
        return original(self, gateway_request, validator=validator, provider=ProviderSpec(FunctionRef(
            'tests.e2e.toc_provider_transport', 'toc_with_test_transport')), **kwargs)

    monkeypatch.setattr(material_toc.AiGateway, 'run', observed)
    monkeypatch.setenv('MATERIAL_E2E_TOC', mode)
    case, conn = material_case, domain_connection
    material = upload(api_client, case, two_page_pdf()).json()['data']['materialId']
    before = snapshot(conn)
    conn.commit()
    response = request(api_client, case, material, '첫 단원 Intro는 첫 쪽, 다음 단원 Details는 둘째 쪽입니다.')
    assert response.status_code == (200 if mode == 'good' else 503), response.text
    assert snapshot(conn) == before
    usage = conn.execute(select(t.ai_usage_logs)).mappings().one()
    assert usage['user_id'] == case['user']
    assert usage['status'] == ('failed' if mode == 'failed' else 'succeeded')
    assert (conn.execute(select(t.ai_saved_results)).first() is not None) is (mode == 'good')
    conn.commit()
    if mode == 'good':
        repeated = request(api_client, case, material, '첫 단원 Intro는 첫 쪽, 다음 단원 Details는 둘째 쪽입니다.')
        assert repeated.json() == response.json()
        assert len(conn.execute(select(t.ai_usage_logs)).all()) == 1
