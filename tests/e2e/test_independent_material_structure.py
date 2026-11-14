"""Independent HTTP/DB oracles for material identity and plan isolation."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_units_contract import unit_case, add_unit, add_concept
from tests.test_material_structure_e2e import body_for, put


def test_metadata_then_concept_replacement_keeps_only_unaffected_identity(api_client, domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    first = add_unit(conn, case, mastery_score=81, mastery_level='high', recovery_mode=True)
    first_concept = add_concept(conn, first)
    second = add_unit(conn, case, no=2, start=13, end=30, order=2, mastery_score=73)
    second_concept = add_concept(conn, second, start=13, end=30)
    conn.commit()
    body = body_for(api_client, case)
    body['units'][0].update(no=9, title='renamed', difficulty='high', inExam=False)
    body['units'][0]['concepts'][0]['title'] = 'renamed concept'
    response = put(api_client, case, body)
    assert response.status_code == 200, response.text
    current = {row['id']: row for row in response.json()['data']['structure']['units']}
    assert set(current) == {first, second}
    assert current[first]['concepts'][0]['id'] == first_concept
    old = dict(conn.execute(select(t.units).where(t.units.c.id == first)).mappings().one())
    assert (old['mastery_score'], old['mastery_level'], old['recovery_mode']) == (81, 'high', True)
    assert current[first]['evidence'] == ['PDF에서 확인한 목차']
    conn.commit()
    body = body_for(api_client, case)
    edited = next(row for row in body['units'] if row['id'] == first)
    edited['concepts'].append({'title': 'new concept', 'from': 6, 'to': 12, 'inExam': True})
    response = put(api_client, case, body)
    assert response.status_code == 200, response.text
    current = response.json()['data']['structure']['units']
    replacement = next(row for row in current if row['no'] == 9)
    assert replacement['id'] not in {first, second}
    assert first_concept not in {row['id'] for row in replacement['concepts']}
    assert replacement['evidence'] == [] and replacement['quizless'] is True
    unaffected = next(row for row in current if row['id'] == second)
    assert unaffected['concepts'][0]['id'] == second_concept
    preserved = dict(conn.execute(select(t.units).where(t.units.c.id == first)).mappings().one())
    assert preserved['retired_at'] is not None
    assert all(preserved[key] == old[key] for key in old if key not in {'retired_at', 'updated_at'})
    assert conn.scalar(select(t.concepts.c.retired_at).where(t.concepts.c.id == first_concept)) is not None
    fresh = conn.execute(select(t.units).where(t.units.c.id == replacement['id'])).mappings().one()
    assert fresh['mastery_score'] is None and fresh['mastery_level'] is None and fresh['recovery_mode'] is False
    assert conn.scalar(select(t.units.c.mastery_score).where(t.units.c.id == second)) == 73


@pytest.mark.parametrize('invalid', ['foreign_unit', 'foreign_concept', 'duplicate_unit', 'duplicate_concept', 'overlap', 'outside_concept'])
def test_rejected_candidate_never_partially_writes_structure(api_client, domain_connection, unit_case, invalid):
    conn, case = domain_connection, unit_case
    unit = add_unit(conn, case)
    concept = add_concept(conn, unit)
    foreign_unit = add_unit(conn, case, material='other_material')
    foreign_concept = add_concept(conn, foreign_unit)
    before = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all())
    conn.commit()
    body = body_for(api_client, case)
    row = body['units'][0]
    row['title'] = 'must never save'
    if invalid == 'foreign_unit': row['id'] = foreign_unit
    if invalid == 'foreign_concept': row['concepts'][0]['id'] = foreign_concept
    if invalid == 'duplicate_unit':
        duplicate = deepcopy(row)
        duplicate.update(no=2, **{'from': 13, 'to': 30, 'concepts': []})
        body['units'].append(duplicate)
    if invalid == 'duplicate_concept': row['concepts'].append(deepcopy(row['concepts'][0]))
    if invalid == 'overlap': body['units'].append({'no': 2, 'title': 'overlap', 'from': 12, 'to': 13, 'inExam': True, 'concepts': []})
    if invalid == 'outside_concept': row['concepts'][0]['to'] = 13
    response = put(api_client, case, body)
    assert response.status_code == 422, response.text
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all()) == before

