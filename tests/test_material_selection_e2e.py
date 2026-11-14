import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_units_contract import unit_case, add_unit, add_concept
from tests.test_plan_draft import plan_case
from tests.test_plan_addition_e2e import addition_case
from tests.test_material_structure_e2e import body_for, put


def patch(client, case, body, *, material=None, user=None):
    return client.patch(f"/api/materials/{material or case['material']}/units",
        headers={'X-Anonymous-Id': user or case['user']}, json=body)


def test_draft_selection_keeps_hidden_concept_choices(api_client, domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    unit = add_unit(conn, case)
    concept = add_concept(conn, unit)
    conn.commit()
    response = patch(api_client, case, {'unitId': unit, 'inExam': False})
    assert response.status_code == 200, response.text
    current = response.json()['data']['structure']['units'][0]
    assert response.json()['data']['previewId'] is None
    assert current['id'] == unit and current['inExam'] is False
    assert current['concepts'][0]['id'] == concept and current['concepts'][0]['inExam'] is True
    assert patch(api_client, case, {'unitId': unit, 'inExam': True}).json()['data']['structure']['units'][0]['concepts'][0]['inExam'] is True


@pytest.mark.parametrize('invalid', ['null', 'empty', 'duplicate', 'foreign_concept'])
def test_selection_rejects_ambiguous_input_without_mutation(api_client, domain_connection, unit_case, invalid):
    conn, case = domain_connection, unit_case
    unit = add_unit(conn, case)
    concept = add_concept(conn, unit)
    other = add_unit(conn, case, material='other_material')
    foreign = add_concept(conn, other)
    before = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all())
    conn.commit()
    bodies = {'null': {'unitId': unit, 'inExam': None}, 'empty': {'unitId': unit},
        'duplicate': {'unitId': unit, 'concepts': [{'conceptId': concept, 'inExam': True}] * 2},
        'foreign_concept': {'unitId': unit, 'concepts': [{'conceptId': foreign, 'inExam': True}]}}
    response = patch(api_client, case, bodies[invalid])
    assert response.status_code == 422, response.text
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all()) == before


def test_approved_selection_requires_preview_then_replays_exact_result(api_client, domain_connection, addition_case):
    conn, case = domain_connection, addition_case
    first = add_concept(conn, case['unit'], start=1, end=12, order=1)
    second = add_concept(conn, case['unit'], start=13, end=20, order=2)
    conn.commit()
    body = {'unitId': case['unit'], 'concepts': [{'conceptId': second, 'inExam': False}]}
    response = patch(api_client, case, body)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['applied'] is False
    assert conn.scalar(select(t.concepts.c.in_scope).where(t.concepts.c.id == second)) is True
    conn.commit()
    assert patch(api_client, case, body).json() == response.json()
    assert put(api_client, case, {'previewId': value['previewId']}).status_code == 404
    assert patch(api_client, case, {'previewId': value['previewId']}, material=case['added_material']).status_code == 404
    assert patch(api_client, case, {'previewId': value['previewId']}, user=case['other_user']).status_code == 404
    approved = patch(api_client, case, {'previewId': value['previewId']})
    assert approved.status_code == 200, approved.text
    assert approved.json()['data']['applied'] is True
    assert approved.json()['data']['structure']['units'][0]['id'] == case['unit']
    assert patch(api_client, case, {'previewId': value['previewId']}).json() == approved.json()
    assert conn.scalar(select(t.concepts.c.in_scope).where(t.concepts.c.id == second)) is False
