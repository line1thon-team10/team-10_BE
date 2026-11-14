from copy import deepcopy

import pytest
from sqlalchemy import select, update

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.schemas.units import UnitsEdit, UnitSelection
from app.services.unit_edits import prepare_structure, persist_structure, candidate_view, selection_as_edit
from app.services.units import UnitService
from tests.test_units_contract import unit_case, add_unit, add_concept


def edit_body(conn, case):
    result = UnitService(conn).get(case['user'], case['subject'], case['material'])
    result.pop('file')
    for unit in result['units']:
        unit.pop('evidence')
        unit.pop('quizless')
    return result


def test_metadata_updates_preserve_ids_mastery_and_manual_selection(domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    unit = add_unit(conn, case, mastery_score=82)
    concept = add_concept(conn, unit)
    body = edit_body(conn, case)
    body['units'][0].update(no=2, title='Renamed', difficulty='high', inExam=False)
    body['units'][0]['concepts'][0].update(title='Renamed concept', inExam=False)
    candidate = prepare_structure(conn, case['user'], UnitsEdit.model_validate(body))
    assert not candidate['retireUnitIds'] and not candidate['retireConceptIds']
    result = persist_structure(conn, case['user'], candidate, now_utc())
    assert result['units'][0]['id'] == unit and result['units'][0]['concepts'][0]['id'] == concept
    assert result['units'][0]['no'] == 2 and result['units'][0]['inExam'] is False
    assert result['units'][0]['concepts'][0]['inExam'] is False
    assert conn.scalar(select(t.units.c.mastery_score).where(t.units.c.id == unit)) == 82


@pytest.mark.parametrize('change', ['unit_range', 'concept_range', 'add_concept', 'delete_concept'])
def test_structure_changes_retire_only_affected_unit_without_copying_mastery(domain_connection, unit_case, change):
    conn, case = domain_connection, unit_case
    first = add_unit(conn, case, mastery_score=82)
    concept = add_concept(conn, first)
    second = add_unit(conn, case, no=2, start=13, end=30, order=2, mastery_score=55)
    body = edit_body(conn, case)
    if change == 'unit_range': body['units'][0]['to'] = 11
    if change == 'concept_range': body['units'][0]['concepts'][0]['to'] = 4
    if change == 'add_concept': body['units'][0]['concepts'].append({'title': 'New', 'from': 4, 'to': 7, 'inExam': True})
    if change == 'delete_concept': body['units'][0]['concepts'] = []
    candidate = prepare_structure(conn, case['user'], UnitsEdit.model_validate(body))
    assert candidate['retireUnitIds'] == [first] and candidate['retireConceptIds'] == [concept]
    preview = candidate_view(conn, case['user'], candidate)
    assert preview['units'][0]['quizless'] and preview['units'][0]['evidence'] == []
    assert conn.scalar(select(t.units.c.retired_at).where(t.units.c.id == first)) is None
    result = persist_structure(conn, case['user'], candidate, now_utc())
    assert result['units'][0]['id'] != first and result['units'][1]['id'] == second
    rows = {row['id']: row for row in conn.execute(select(t.units)).mappings()}
    assert rows[first]['retired_at'] is not None and rows[first]['mastery_score'] == 82
    assert rows[result['units'][0]['id']]['mastery_score'] is None
    assert rows[second]['mastery_score'] == 55
    assert conn.scalar(select(t.concepts.c.retired_at).where(t.concepts.c.id == concept)) is not None


def test_selection_preserves_ids_and_hidden_concept_choices(domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    first = add_unit(conn, case)
    concept = add_concept(conn, first)
    body = UnitSelection.model_validate({'unitId': first, 'inExam': False})
    edit = selection_as_edit(conn, case['user'], case['material'], body)
    assert edit.units[0].concepts[0].inExam is True
    candidate = prepare_structure(conn, case['user'], edit)
    result = persist_structure(conn, case['user'], candidate, now_utc())
    assert result['units'][0]['id'] == first and result['units'][0]['inExam'] is False
    assert result['units'][0]['concepts'][0]['id'] == concept
    assert result['units'][0]['concepts'][0]['inExam'] is True


@pytest.mark.parametrize('case_name', ['empty', 'overlap', 'outside', 'concept_outside', 'duplicate_no',
    'duplicate_unit', 'foreign_unit', 'duplicate_concept', 'foreign_concept', 'blank_title'])
def test_invalid_edits_do_not_mutate_existing_structure(domain_connection, unit_case, case_name):
    conn, case = domain_connection, unit_case
    first = add_unit(conn, case)
    add_concept(conn, first)
    foreign = add_unit(conn, case, material='other_material')
    foreign_concept = add_concept(conn, foreign)
    second = add_unit(conn, case, no=2, start=13, end=30, order=2)
    body = edit_body(conn, case)
    if case_name == 'empty': body['units'] = []
    if case_name == 'overlap': body['units'][1]['from'] = 12
    if case_name == 'outside': body['units'][1]['to'] = 31
    if case_name == 'concept_outside': body['units'][0]['concepts'][0]['to'] = 13
    if case_name == 'duplicate_no': body['units'][1]['no'] = 1
    if case_name == 'duplicate_unit': body['units'][1]['id'] = first
    if case_name == 'foreign_unit': body['units'][0]['id'] = foreign
    if case_name == 'duplicate_concept': body['units'][0]['concepts'] *= 2
    if case_name == 'foreign_concept': body['units'][0]['concepts'][0]['id'] = foreign_concept
    if case_name == 'blank_title': body['units'][0]['title'] = ' '
    before = (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all())
    with pytest.raises(ApiError) as caught:
        prepare_structure(conn, case['user'], UnitsEdit.model_validate(body))
    assert caught.value.status == (400 if case_name == 'empty' else 422)
    assert (conn.execute(select(t.units)).all(), conn.execute(select(t.concepts)).all()) == before


def test_candidate_and_saved_structure_have_the_same_unit_order(domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    add_unit(conn, case)
    add_unit(conn, case, no=2, start=13, end=30, order=2)
    body = edit_body(conn, case)
    body['units'].reverse()
    candidate = prepare_structure(conn, case['user'], UnitsEdit.model_validate(body))
    preview = candidate_view(conn, case['user'], candidate)
    saved = persist_structure(conn, case['user'], candidate, now_utc())
    assert preview == saved
