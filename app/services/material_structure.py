"""Draft structure edits and approved-plan previews share immutable candidates."""
from collections import defaultdict
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import PageSpan, page_count
from app.domain.scheduling import POLICY_VERSION, SearchLimit
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.materials import MaterialRepository
from app.repositories.proposals import LegacyProposalUnavailable, LegacyResponseUnavailable, ProposalNotFound, ProposalRepository
from app.services.allocator import allocate
from app.services.plan_draft import _apply as apply_placements, _basis
from app.services.plan_inputs import PlanInputError, PendingObligations, existing_allocation_input
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from app.services.unit_edits import candidate_view, prepare_structure, persist_structure, selection_as_edit


def _apply(conn, context, changes):
    structure = persist_structure(conn, context.user.id, changes['structure'], context.now)
    apply_placements(conn, context, changes['allocation'])
    return {'applied': True, 'previewId': changes['previewId'], 'structure': structure}


DOMAINS = {operation: PreviewDomain('material_structure_' + operation, PlanEffect.UPDATE, _basis, _apply)
           for operation in ('put', 'patch')}


def _lock(conn, user_id):
    try:
        if lock_user(conn, UUID(user_id)) is None:
            raise ApiError('UNKNOWN_SESSION', '사용자를 찾지 못했어요', 401)
    except UserBusyError:
        raise ApiError('REQUEST_IN_PROGRESS', '다른 변경이 진행 중이에요. 잠시 후 다시 시도해 주세요', 409) from None


def _span(material_id, start, end):
    try:
        return PageSpan(material_id, start, end)
    except ValueError:
        raise ApiError('INVALID_INPUT', '기존 목차의 PDF 페이지 범위를 확인해 주세요', 422) from None


def _exam_pages(units, concepts):
    by_unit = defaultdict(list)
    for concept in concepts:
        by_unit[concept['unit_id']].append(concept)
    spans = []
    for unit in units:
        if not unit['in_scope']:
            continue
        children = by_unit[unit['id']]
        if children:
            spans.extend(_span(unit['material_id'], child['start_page'], child['end_page'])
                         for child in children if child['in_scope'])
        else:
            spans.append(_span(unit['material_id'], unit['start_page'], unit['end_page']))
    return page_count(spans)


def _ready(service, user_id, snapshot, normalized):
    for candidate in service.repository.ready(user_id, kind=snapshot.kind):
        if candidate['target'] != snapshot.target or candidate['normalized_input'] != normalized:
            continue
        try:
            service._require_fresh(candidate, snapshot)
        except ApiError:
            continue
        return candidate['preview_response']
    return None


def _fraction(value):
    return [value.numerator, value.denominator]


def _placements(request, result, candidate, conn):
    titles = {row['id']: row['title'] for row in conn.execute(select(t.units).where(
        t.units.c.id.in_([unit.id for unit in request.units]))).mappings()}
    titles.update({row['id']: row['title'] for row in candidate['units']})
    return [{'id': str(uuid4()), 'subjectId': task.subject_id, 'kind': task.kind,
        'date': task.day.isoformat(), 'start': _fraction(task.start), 'end': _fraction(task.end),
        'minutes': task.display_minutes, 'title': ' · '.join(dict.fromkeys(titles[part.unit_id] for part in task.parts)),
        'parts': [{'requirementId': part.requirement_id, 'unitId': part.unit_id, 'materialId': part.span.material_id,
            'from': part.span.start, 'to': part.span.end, 'baseline': _fraction(part.baseline_minutes_per_page),
            'multiplier': _fraction(part.difficulty_multiplier)} for part in task.parts]} for task in result.placements]


class MaterialStructureService:
    def __init__(self, engine):
        self.engine = engine

    def edit(self, user_id, body, *, operation='put', material_id=None):
        if operation not in DOMAINS:
            raise ValueError('Unknown structure operation')
        normalized = {'request': body.model_dump(by_alias=True, exclude_unset=True)}
        with self.engine.begin() as conn:
            _lock(conn, user_id)
            request_body = selection_as_edit(conn, user_id, material_id, body) if operation == 'patch' else body
            candidate = prepare_structure(conn, user_id, request_body)
            material = MaterialRepository(conn).material(user_id, candidate['materialId'])
            subject = MaterialRepository(conn).subject(user_id, material['subject_id'])
            approved = subject['status'] == 'active' and conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id == user_id)) is not None
            if not approved:
                structure = persist_structure(conn, user_id, candidate, now_utc())
                return {'applied': True, 'previewId': None, 'structure': structure}
            target = {'materialId': material['id'], 'subjectId': subject['id'], 'operation': operation}
            service, domain = ProposalService(conn), DOMAINS[operation]
            snapshot = service.capture(user_id, domain=domain, target=target)
            ready = _ready(service, user_id, snapshot, normalized)
            if ready is not None:
                return ready
            before_units, before_concepts = service_units(conn, material['id'])
            before_pages = _exam_pages(before_units, before_concepts)
            after_pages = _exam_pages(candidate['units'], candidate['concepts'])
            other_materials = list(conn.scalars(select(t.materials.c.id).where(t.materials.c.subject_id == subject['id'],
                                                                           t.materials.c.id != material['id'])))
            remaining_pages = after_pages
            for identifier in other_materials:
                remaining_pages += _exam_pages(*service_units(conn, identifier))
            if remaining_pages == 0:
                raise ApiError('PLAN_INFEASIBLE', '과목의 시험 범위를 모두 제외할 수 없어요', 409)
            timezone_name = conn.scalar(select(t.users.c.timezone).where(t.users.c.id == user_id))
            try:
                request, supersede, _ = existing_allocation_input(conn, user_id, today=snapshot.base_date,
                    timezone_name=timezone_name, subject_ids=(), affected_subject_ids=(subject['id'],),
                    material_structure_overrides={material['id']: {
                        'units': candidate['units'], 'concepts': candidate['concepts']}})
            except PendingObligations as error:
                raise ApiError('OVERDUE_OBLIGATIONS', str(error), 409) from None
            except PlanInputError as error:
                raise ApiError('INVALID_INPUT', str(error), 422) from None
        try:
            result = allocate(request)
        except SearchLimit:
            raise ApiError('PLAN_SEARCH_LIMIT', '계획 탐색 한도에 도달했어요. 다시 요청해 주세요', 503) from None
        if not result.feasible:
            raise ApiError('PLAN_INFEASIBLE', ' '.join(reason.message for reason in result.reasons) or '남은 시간에 배정할 수 없어요', 409)
        with self.engine.begin() as conn:
            service = ProposalService(conn)
            current = service.capture(user_id, domain=domain, target=target)
            if current != snapshot:
                raise ApiError('PREVIEW_STALE', '계획을 계산하는 동안 자료가 바뀌었어요. 다시 요청해 주세요', 409)
            ready = _ready(service, user_id, snapshot, normalized)
            if ready is not None:
                return ready
            placements = _placements(request, result, candidate, conn)
            days = defaultdict(lambda: [0, 0])
            for placement in placements:
                days[placement['date']][0] += 1
                days[placement['date']][1] += placement['minutes']
            identifier = str(uuid4())
            response = {'applied': False, 'previewId': identifier, 'structure': candidate_view(conn, user_id, candidate),
                'changes': [{'label': '단원 수', 'before': str(len(before_units)), 'after': str(len(candidate['units']))},
                            {'label': '시험 범위', 'before': f'{before_pages}쪽', 'after': f'{after_pages}쪽'}],
                'days': [{'date': day, 'text': f'{count}개 태스크 · {minutes}분'} for day, (count, minutes) in sorted(days.items())],
                'note': ' '.join(reason.message for reason in result.reasons) or None}
            return service.create(user_id, domain=domain, target=target, snapshot=snapshot,
                normalized_input=normalized, changes={'previewId': identifier, 'structure': candidate,
                    'allocation': {'schemaVersion': 'plan-placements-v1', 'policy': POLICY_VERSION,
                        'timezone': timezone_name, 'activateSubjectIds': [], 'supersedeIds': supersede, 'placements': placements}},
                preview_response=response, preview_id=identifier, proposal_type='material', description='자료 구조와 남은 학습 범위 변경')

    def approve(self, user_id, preview_id, *, operation='put', material_id=None):
        with self.engine.begin() as conn:
            try:
                candidate = ProposalRepository(conn).get(user_id, preview_id)
            except ProposalNotFound as error:
                raise ApiError('NO_PREVIEW', '미리보기를 찾지 못했어요', 404 if error.foreign else 400) from None
            except (LegacyProposalUnavailable, LegacyResponseUnavailable):
                raise ApiError('PREVIEW_STALE', '과거 후보의 기준을 확인할 수 없어요', 409) from None
            domain = DOMAINS[operation]
            if candidate['kind'] != domain.kind or (material_id is not None and candidate['target'].get('materialId') != material_id):
                raise ApiError('NO_PREVIEW', '이 자료 편집의 미리보기가 아니에요', 404)
            return ProposalService(conn).apply(user_id, preview_id, domain=domain, target=candidate['target'])


def service_units(conn, material_id):
    from app.repositories.units import UnitRepository
    return UnitRepository(conn).active_structure(material_id)
