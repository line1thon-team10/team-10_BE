"""Subject creation, persisted cards and onboarding over actual API transactions."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.clock import now_utc
from app.db import schema as t


@pytest.fixture
def empty_account(api_client, monkeypatch):
    from app.routers import session
    monkeypatch.setattr(session, 'DEMO_MODE', False)
    response = api_client.post('/api/session')
    assert response.status_code == 200
    return {'X-Anonymous-Id': response.json()['data']['anonymousId']}


def test_empty_to_draft_to_onboarding_is_persistent_and_owned(api_client, empty_account, domain_connection):
    headers, conn = empty_account, domain_connection
    assert api_client.get('/api/subjects', headers=headers).json()['data']['subjects'] == []
    response = api_client.post('/api/subjects', headers=headers, json={'name': '  자료구조  '})
    assert response.status_code == 200, response.text
    created = response.json()['data']
    assert created['status'] == 'draft'
    card = api_client.get('/api/subjects', headers=headers).json()['data']['subjects'][0]
    assert card['id'] == created['subjectId'] and card['name'] == '자료구조'
    assert all(card[key] is None for key in ('examDate', 'dday', 'goal', 'goalTotal', 'target'))
    assert card['progress'] == {'studied': 0, 'total': 0, 'rate': 0}
    assert card['range'] == {'from': None, 'to': None, 'pages': 0}
    onboarding = api_client.get('/api/onboarding', params={'subjectId': card['id']}, headers=headers)
    assert onboarding.status_code == 200 and onboarding.json()['data']['done'] is False
    assert conn.execute(select(t.plans)).first() is None
    assert conn.execute(select(t.study_tasks)).first() is None
    conn.commit()
    other = api_client.post('/api/session').json()['data']['anonymousId']
    assert api_client.get('/api/subjects', headers={'X-Anonymous-Id': other}).json()['data']['subjects'] == []
    assert api_client.get('/api/onboarding', params={'subjectId': card['id']},
        headers={'X-Anonymous-Id': other}).status_code == 404


@pytest.mark.parametrize('payload,expected', [({'name': ''},400), ({'name':' \n\t'},400),
    ({'name':None},422), ({'name':123},422), ({'name':True},422), ({},422),
    ({'name':'valid','status':'active'},422), ({'name':'\x00'},422)])
def test_invalid_creation_leaves_no_partial_subject(api_client, empty_account, domain_connection, payload, expected):
    response = api_client.post('/api/subjects', headers=empty_account, json=payload)
    assert response.status_code == expected, response.text
    assert domain_connection.execute(select(t.subjects)).first() is None


def test_multi_pdf_concept_union_uses_shared_progress_and_preserves_plan(
        api_client, demo_user, domain_connection):
    conn, headers = domain_connection, {'X-Anonymous-Id': demo_user}
    before = {table.name: conn.execute(select(table)).all()
              for table in (t.plans, t.study_tasks, t.study_records, t.learning_passes)}
    conn.commit()
    cards = api_client.get('/api/subjects', headers=headers)
    assert cards.status_code == 200, cards.text
    home = api_client.get('/api/today', headers=headers).json()['data']
    readiness = {row['subjectId']: row['readiness'] for row in home['exams']}
    assert all(card['readiness'] == readiness[card['id']] for card in cards.json()['data']['subjects'])
    created = api_client.post('/api/subjects', headers=headers, json={'name':'추가 과목'}).json()['data']
    sid, now = created['subjectId'], now_utc()
    materials = []
    for index in range(2):
        mid, uid = str(uuid4()), str(uuid4())
        materials.append(mid)
        conn.execute(t.materials.insert().values(id=mid, subject_id=sid, original_name=f'{index}.pdf',
            source_format='pdf', sha256=mid, page_count=10, analysis_version='v1', created_at=now, updated_at=now))
        conn.execute(t.units.insert().values(id=uid, material_id=mid, code='unit_1', title='단원',
            sort_order=1, start_page=1,end_page=10, created_at=now, updated_at=now))
        for no, start, end, included in ((1,1,3,True),(2,2,5,True),(3,8,10,False)):
            conn.execute(t.concepts.insert().values(id=str(uuid4()),unit_id=uid,code=str(no),title='개념',
                sort_order=no,start_page=start,end_page=end,in_scope=included,created_at=now,updated_at=now))
    conn.commit()
    cards = api_client.get('/api/subjects', headers=headers).json()['data']['subjects']
    card = next(row for row in cards if row['id'] == sid)
    assert card['range'] == {'from':None,'to':None,'pages':10}
    assert card['progress'] == {'studied':0,'total':10,'rate':0}
    assert {row['id'] for row in card['materials']} == set(materials)
    assert all(row['toc'][0]['no'] == 1 and row['toc'][0]['unitId'] for row in card['materials'])
    for table in (t.plans, t.study_tasks, t.study_records, t.learning_passes):
        assert conn.execute(select(table)).all() == before[table.name]
