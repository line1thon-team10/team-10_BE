"""Prepare and persist material-local structures without deleting historical IDs."""
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.units import UnitRepository
from app.schemas.units import UnitsEdit
from app.services.units import UnitService


def invalid(message='단원과 개념의 ID 및 페이지 범위를 확인해 주세요'):
    return ApiError('INVALID_INPUT', message, 422)


def _title(value):
    if not value.strip() or '\x00' in value:
        raise invalid('단원과 개념의 제목을 입력해 주세요')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise invalid('제목 문자열을 확인해 주세요') from None
    return value.strip()


def prepare_structure(conn, user_id, body):
    repo = UnitRepository(conn)
    material = repo.material(user_id, body.subjectId, body.materialId)
    old_units, old_concepts = repo.active_structure(body.materialId)
    by_unit = {row['id']: row for row in old_units}
    by_concept = {row['id']: row for row in old_concepts}
    if not body.units:
        raise ApiError('NO_UNITS', '단원을 한 개 이상 입력해 주세요', 400)
    pages = material['page_count']
    if type(pages) is not int or pages < 1:
        raise invalid('실제 PDF 페이지 수를 먼저 확인해 주세요')
    numbers, ids, concept_ids, intervals = set(), set(), set(), []
    units, concepts = [], []
    for item in body.units:
        if item.no in numbers or not 1 <= item.from_ <= item.to <= pages:
            raise invalid()
        numbers.add(item.no)
        intervals.append((item.from_, item.to))
        old = by_unit.get(item.id) if item.id else None
        if item.id and (old is None or item.id in ids):
            raise invalid()
        if item.id:
            ids.add(item.id)
        previous_concepts = {row['id'] for row in old_concepts if old and row['unit_id'] == old['id']}
        for concept in item.concepts:
            if not item.from_ <= concept.from_ <= concept.to <= item.to:
                raise invalid()
            if concept.id:
                previous = by_concept.get(concept.id)
                if (previous is None or not old or previous['unit_id'] != old['id'] or concept.id in concept_ids):
                    raise invalid()
                concept_ids.add(concept.id)
        supplied_concepts = {concept.id for concept in item.concepts if concept.id}
        replaced = (old is None or (old['start_page'], old['end_page']) != (item.from_, item.to)
            or supplied_concepts != previous_concepts or any(concept.id is None for concept in item.concepts)
            or any((by_concept[concept.id]['start_page'], by_concept[concept.id]['end_page']) !=
                   (concept.from_, concept.to) for concept in item.concepts if concept.id))
        unit_id = str(uuid4()) if replaced else old['id']
        row = {'id': unit_id, 'material_id': material['id'], 'code': f'unit_{item.no:02d}',
            'title': _title(item.title), 'sort_order': item.no, 'quantity_basis': 'page',
            'start_page': item.from_, 'end_page': item.to, 'difficulty': item.difficulty,
            'in_scope': item.inExam, 'evidence': [] if replaced else deepcopy(old['evidence'] or []),
            'mastery_score': None if replaced else old['mastery_score']}
        units.append(row)
        for index, concept in enumerate(item.concepts, 1):
            concepts.append({'id': str(uuid4()) if replaced else concept.id, 'unit_id': unit_id,
                'code': f'concept_{index:02d}', 'title': _title(concept.title), 'sort_order': index,
                'start_page': concept.from_, 'end_page': concept.to, 'in_scope': concept.inExam})
    intervals.sort()
    if any(right[0] <= left[1] for left, right in zip(intervals, intervals[1:])):
        raise invalid('단원 페이지 범위가 겹치지 않게 입력해 주세요')
    units.sort(key=lambda row: row['sort_order'])
    return {'subjectId': body.subjectId, 'materialId': body.materialId, 'units': units, 'concepts': concepts,
        'retireUnitIds': [row['id'] for row in old_units if row['id'] not in {unit['id'] for unit in units}],
        'retireConceptIds': [row['id'] for row in old_concepts if row['id'] not in {concept['id'] for concept in concepts}]}


def selection_as_edit(conn, user_id, material_id, body):
    from app.repositories.materials import MaterialRepository
    material = MaterialRepository(conn).material(user_id, material_id)
    structure = UnitService(conn).get(user_id, material['subject_id'], material_id)
    if ('inExam' in body.model_fields_set and body.inExam is None
            or body.inExam is None and not body.concepts):
        raise invalid('단원 또는 개념의 시험 범위를 선택해 주세요')
    targets = [unit for unit in structure['units'] if unit['id'] == body.unitId]
    if not targets:
        raise invalid()
    target = targets[0]
    if body.inExam is not None:
        target['inExam'] = body.inExam
    concepts = {row['id']: row for row in target['concepts']}
    seen = set()
    for choice in body.concepts:
        if choice.conceptId in seen or choice.conceptId not in concepts:
            raise invalid()
        seen.add(choice.conceptId)
        concepts[choice.conceptId]['inExam'] = choice.inExam
    for unit in structure['units']:
        unit.pop('evidence')
        unit.pop('quizless')
    structure.pop('file')
    return UnitsEdit.model_validate(structure)


def candidate_view(conn, user_id, candidate):
    material = UnitRepository(conn).material(user_id, candidate['subjectId'], candidate['materialId'])
    return UnitService.render(material, candidate['units'], candidate['concepts'])


def persist_structure(conn, user_id, candidate, now):
    repo = UnitRepository(conn)
    material = repo.material(user_id, candidate['subjectId'], candidate['materialId'])
    units, concepts = repo.active_structure(material['id'])
    unit_ids, concept_ids = {row['id'] for row in units}, {row['id'] for row in concepts}
    if candidate['retireConceptIds']:
        conn.execute(update(t.concepts).where(t.concepts.c.id.in_(candidate['retireConceptIds']),
            t.concepts.c.id.in_(concept_ids)).values(retired_at=now, updated_at=now))
    if candidate['retireUnitIds']:
        conn.execute(update(t.units).where(t.units.c.id.in_(candidate['retireUnitIds']),
            t.units.c.id.in_(unit_ids)).values(retired_at=now, updated_at=now))
    for row in candidate['units']:
        if row['id'] in unit_ids:
            conn.execute(update(t.units).where(t.units.c.id == row['id']).values(**row, updated_at=now))
        else:
            conn.execute(t.units.insert().values(**row, created_at=now, updated_at=now))
    for row in candidate['concepts']:
        if row['id'] in concept_ids:
            conn.execute(update(t.concepts).where(t.concepts.c.id == row['id']).values(**row, updated_at=now))
        else:
            conn.execute(t.concepts.insert().values(**row, created_at=now, updated_at=now))
    conn.execute(update(t.materials).where(t.materials.c.id == material['id']).values(updated_at=now))
    return UnitService(conn).get(user_id, candidate['subjectId'], candidate['materialId'])
