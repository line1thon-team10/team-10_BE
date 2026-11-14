"""Read-only allocation previews and atomic approval of immutable placements."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import page_count
from app.domain.scheduling import POLICY_VERSION, SearchLimit, half_up
from app.repositories.proposals import (
    LegacyProposalUnavailable, LegacyResponseUnavailable, ProposalNotFound,
    ProposalRepository, canonical_hash,
)
from app.services.allocator import allocate
from app.services.plan_inputs import PlanInputError, PendingObligations, initial_allocation_input, existing_allocation_input
from app.services.proposals import DomainBasis, PlanEffect, PreviewDomain, ProposalService


KIND = 'initial_plan'


def _json_value(value):
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, dict):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _rows(conn, query):
    return [_json_value(dict(row)) for row in conn.execute(query).mappings()]


def _basis(conn, context, target):
    subjects = select(t.subjects.c.id).where(t.subjects.c.user_id == context.user.id)
    materials = select(t.materials.c.id).where(t.materials.c.subject_id.in_(subjects))
    units = select(t.units.c.id).where(t.units.c.material_id.in_(materials))
    events = select(t.calendar_events.c.id).where(t.calendar_events.c.user_id == context.user.id)
    data = {'policy': POLICY_VERSION, 'snapshotVersion': 'plan-input-v1', 'timezone': context.user.timezone,
        'target': target,
        'subjects': _rows(conn, select(t.subjects).where(t.subjects.c.id.in_(subjects)).order_by(t.subjects.c.id)),
        'materials': _rows(conn, select(t.materials).where(t.materials.c.id.in_(materials)).order_by(t.materials.c.id)),
        'units': _rows(conn, select(t.units).where(t.units.c.id.in_(units)).order_by(t.units.c.id)),
        'concepts': _rows(conn, select(t.concepts).where(t.concepts.c.unit_id.in_(units)).order_by(t.concepts.c.id)),
        'events': _rows(conn, select(t.calendar_events).where(t.calendar_events.c.id.in_(events)).order_by(t.calendar_events.c.id)),
        'exceptions': _rows(conn, select(t.calendar_event_exceptions).where(t.calendar_event_exceptions.c.event_id.in_(events))
                            .order_by(t.calendar_event_exceptions.c.id))}
    tasks = select(t.study_tasks.c.id).where(t.study_tasks.c.subject_id.in_(subjects))
    records = select(t.study_records.c.id).where(t.study_records.c.task_id.in_(tasks))
    obligations = select(t.remedial_obligations.c.id).where(t.remedial_obligations.c.user_id == context.user.id)
    data.update({
        'remedialObligations': _rows(conn, select(t.remedial_obligations).where(t.remedial_obligations.c.id.in_(obligations)).order_by(t.remedial_obligations.c.id)),
        'remedialBlocks': _rows(conn, select(t.remedial_obligation_blocks).where(t.remedial_obligation_blocks.c.obligation_id.in_(obligations)).order_by(t.remedial_obligation_blocks.c.obligation_id, t.remedial_obligation_blocks.c.block_id)),
        'tasks': _rows(conn, select(t.study_tasks).where(t.study_tasks.c.id.in_(tasks)).order_by(t.study_tasks.c.id)),
        'taskRanges': _rows(conn, select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(tasks)).order_by(t.study_task_ranges.c.id)),
        'records': _rows(conn, select(t.study_records).where(t.study_records.c.id.in_(records)).order_by(t.study_records.c.id)),
        'recordRanges': _rows(conn, select(t.study_record_ranges).where(t.study_record_ranges.c.record_id.in_(records)).order_by(t.study_record_ranges.c.id)),
        'sessions': _rows(conn, select(t.study_sessions).where(t.study_sessions.c.user_id == context.user.id).order_by(t.study_sessions.c.id)),
    })
    digest = canonical_hash(data)
    return DomainBasis({'policy': POLICY_VERSION, 'inputHash': digest},
                       structure_hash=digest if context.plan is None else None)


def _fraction(value):
    return [value.numerator, value.denominator]


def _instant(day, minute, timezone_name):
    local = datetime.combine(date.fromisoformat(day), time(), ZoneInfo(timezone_name))
    # Microsecond timestamps are display/occupancy boundaries; exact Fraction
    # values remain in the immutable proposal for pace and later rescheduling.
    value = Fraction(*minute) * 60 * 1_000_000
    microseconds = (2 * value.numerator + value.denominator) // (2 * value.denominator)
    return (local + timedelta(microseconds=microseconds)).astimezone(timezone.utc)


def _apply(conn, context, changes):
    if changes.get('schemaVersion') != 'plan-placements-v1' or changes.get('policy') != POLICY_VERSION:
        raise ApiError('PREVIEW_STALE', '계획 후보의 버전이 달라졌어요', 409)
    if changes.get('supersedeIds'):
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id.in_(changes['supersedeIds']),
            t.study_tasks.c.plan_id == context.plan.id).values(status='carried_over'))
    used_blocks = set()
    for task in changes['placements']:
        source = task.get('source')
        obligation_id = None
        if task['kind'] in ('remedial', 'review'):
            requirements = {part.get('requirementId') for part in task['parts']}
            if None in requirements or len(requirements) != 1:
                raise ApiError('PREVIEW_STALE', '보충·복습 의무의 원래 범위를 확인해 주세요', 409)
            identifier = next(iter(requirements))
            try:
                identifier = str(UUID(identifier))
            except (ValueError, TypeError, AttributeError):
                raise ApiError('PREVIEW_STALE', '보충·복습 의무 식별자가 달라요', 409) from None
            original = conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == identifier,
                t.study_tasks.c.plan_id == context.plan.id, t.study_tasks.c.subject_id == task['subjectId'],
                t.study_tasks.c.task_type == task['kind'])).mappings().first()
            if original is not None:
                expected = {'blockId': original['block_id'], 'taskId': original['id']}
                if source is not None and source != expected:
                    raise ApiError('PREVIEW_STALE', '기존 의무의 책 연결이 달라요', 409)
                source = expected
            elif task['kind'] == 'remedial':
                obligation = conn.execute(select(t.remedial_obligations).join(t.units,
                    t.units.c.id == t.remedial_obligations.c.unit_id).join(t.materials,
                    t.materials.c.id == t.units.c.material_id).where(t.remedial_obligations.c.id == identifier,
                    t.remedial_obligations.c.user_id == context.user.id,
                    t.materials.c.subject_id == task['subjectId'])).mappings().first()
                if obligation is None or source is not None:
                    raise ApiError('PREVIEW_STALE', '미배정 보충 의무를 찾지 못했어요', 409)
                obligation_id = identifier
            else:
                raise ApiError('PREVIEW_STALE', '원래 복습 의무를 찾지 못했어요', 409)
        if source is not None:
            original = conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == source['taskId'],
                t.study_tasks.c.plan_id == context.plan.id, t.study_tasks.c.subject_id == task['subjectId'],
                t.study_tasks.c.block_id == source['blockId'], t.study_tasks.c.task_type == task['kind'])).mappings().first()
            if original is None or source['taskId'] not in changes.get('supersedeIds', ()) or source['blockId'] in used_blocks:
                raise ApiError('PREVIEW_STALE', '기존 책은 하나의 유효 배치로만 옮길 수 있어요', 409)
        block_id = source['blockId'] if source else str(uuid4())
        used_blocks.add(block_id)
        parts = task['parts']
        unit_ids = {part['unitId'] for part in parts}
        if source is None:
            conn.execute(t.study_blocks.insert().values(id=block_id, subject_id=task['subjectId'],
                unit_id=next(iter(unit_ids)) if len(unit_ids) == 1 else None, task_type=task['kind'], created_at=context.now))
        conn.execute(t.study_tasks.insert().values(id=task['id'], plan_id=context.plan.id, subject_id=task['subjectId'],
            block_id=block_id, origin_task_id=source['taskId'] if source else None, task_type=task['kind'], title=task['title'], scheduled_date=date.fromisoformat(task['date']),
            starts_at=_instant(task['date'], task['start'], changes['timezone']),
            ends_at=_instant(task['date'], task['end'], changes['timezone']), estimated_minutes=task['minutes'],
            placement_reason=POLICY_VERSION, status='planned', created_at=context.now))
        if obligation_id is not None:
            conn.execute(t.remedial_obligation_blocks.insert().values(obligation_id=obligation_id, block_id=block_id))
        for order, part in enumerate(parts, 1):
            conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=task['id'],
                unit_id=part['unitId'], start_page=part['from'], end_page=part['to'], sort_order=order,
                planned_minutes=half_up(Fraction(*part['baseline']) * Fraction(*part['multiplier']) * (part['to'] - part['from'] + 1))))
    conn.execute(update(t.subjects).where(t.subjects.c.user_id == context.user.id,
        t.subjects.c.id.in_(changes['activateSubjectIds']), t.subjects.c.status == 'draft').values(status='active', updated_at=context.now))
    conn.execute(update(t.users).where(t.users.c.id == context.user.id).values(onboarding_status='completed', updated_at=context.now))
    return {'approved': True}


INITIAL_DOMAIN = PreviewDomain(KIND, PlanEffect.CREATE, _basis, _apply)
ADDITION_DOMAIN = PreviewDomain('draft_subject_addition', PlanEffect.UPDATE, _basis, _apply)


def _fresh_ready(service, user_id, snapshot):
    for candidate in service.repository.ready(user_id, kind=snapshot.kind):
        if candidate['target'] != snapshot.target:
            continue
        try:
            service._require_fresh(candidate, snapshot)
        except ApiError:
            continue
        return candidate['preview_response']
    return None


def _present(conn, user_id, request, result, saved):
    subjects = {row['id']: dict(row) for row in conn.execute(select(t.subjects).where(t.subjects.c.user_id == user_id)).mappings()}
    unit_ids = [unit.id for unit in request.units]
    units = {row['id']: dict(row) for row in conn.execute(select(t.units).where(t.units.c.id.in_(unit_ids))).mappings()}
    totals, week, placements = defaultdict(int), defaultdict(lambda: defaultdict(int)), []
    for task in result.fixed:
        if task.day >= request.start_date:
            totals[task.day] += half_up(task.minutes)
            week[task.day][task.subject_id] += half_up(task.minutes)
    for task in result.placements:
        totals[task.day] += task.display_minutes
        week[task.day][task.subject_id] += task.display_minutes
        placements.append({'id': str(uuid4()), 'allocatorId': task.id, 'subjectId': task.subject_id, 'kind': task.kind,
            'date': task.day.isoformat(), 'start': _fraction(task.start), 'end': _fraction(task.end), 'minutes': task.display_minutes,
            'title': ' · '.join(dict.fromkeys(units[part.unit_id]['title'] for part in task.parts)),
            'parts': [{'requirementId': part.requirement_id, 'unitId': part.unit_id, 'materialId': part.span.material_id,
                'from': part.span.start, 'to': part.span.end, 'baseline': _fraction(part.baseline_minutes_per_page),
                'multiplier': _fraction(part.difficulty_multiplier)} for part in task.parts]})
    current_fixed = [task for task in result.fixed if task.day >= request.start_date]
    allocation = []
    for subject in request.subjects:
        row = subjects[subject.id]
        scoped = [unit for unit in request.units if unit.subject_id == subject.id]
        allocation.append({'subjectId': subject.id, 'name': row['name'], 'color': row['color'], 'goalLabel': row['target_grade'],
            'pages': page_count(span for unit in scoped for span in unit.ranges),
            'tasks': sum(task.subject_id == subject.id for task in (*result.placements, *current_fixed)),
            'units': [{'unitId': unit.id, 'materialId': unit.material_id, 'no': units[unit.id]['sort_order'],
                'title': units[unit.id]['title'], 'pages': page_count(unit.ranges),
                'tasks': sum(any(part.unit_id == unit.id for part in task.parts) for task in result.placements) +
                    sum(any(part.unit_id == unit.id for part in task.ranges) for task in current_fixed)} for unit in scoped]})
    response = {'saved': saved, 'today': request.start_date.isoformat(),
        'summary': {'totalPages': sum(item['pages'] for item in allocation), 'taskCount': len(result.placements) + len(current_fixed),
            'dailyAvgMinutes': half_up(Fraction(sum(totals.values()), len(totals))) if totals else 0,
            'daysToFirstExam': (min(subject.exam_date for subject in request.subjects) - request.start_date).days},
        'allocation': allocation,
        'firstWeek': [{'date': (request.start_date + timedelta(days=offset)).isoformat(),
            'minutes': dict(week[request.start_date + timedelta(days=offset)])} for offset in range(7)],
        'reasons': [reason.message for reason in result.reasons]}
    return response, placements


class PlanDraftService:
    def __init__(self, engine):
        self.engine = engine

    def draft(self, user_id, *, draft_subject_ids=None, saved=False):
        target = {'draftSubjectIds': sorted(draft_subject_ids) if draft_subject_ids is not None else None}
        with self.engine.begin() as conn:
            service = ProposalService(conn)
            existing = conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id == user_id)) is not None
            domain = ADDITION_DOMAIN if existing else INITIAL_DOMAIN
            snapshot = service.capture(user_id, domain=domain, target=target)
            ready = _fresh_ready(service, user_id, snapshot)
            if ready is not None:
                return ready
            try:
                options = dict(today=snapshot.base_date,
                    timezone_name=conn.scalar(select(t.users.c.timezone).where(t.users.c.id == user_id)),
                    subject_ids=tuple(target['draftSubjectIds']) if target['draftSubjectIds'] is not None else None)
                if existing:
                    request, supersede, display_request = existing_allocation_input(conn, user_id, **options)
                else:
                    request = initial_allocation_input(conn, user_id, **options)
                    supersede, display_request = [], request
            except PendingObligations as exc:
                raise ApiError('OVERDUE_OBLIGATIONS', str(exc), 409) from None
            except PlanInputError as exc:
                raise ApiError('INVALID_INPUT', str(exc), 422) from None
        try:
            result = allocate(request)
        except SearchLimit:
            raise ApiError('PLAN_SEARCH_LIMIT', '계획 탐색 한도에 도달했어요. 잠시 후 다시 요청해 주세요', 503) from None
        if not result.feasible:
            raise ApiError('PLAN_INFEASIBLE', ' '.join(reason.message for reason in result.reasons) or '남은 시간에 계획을 배정할 수 없어요', 409)
        with self.engine.begin() as conn:
            service = ProposalService(conn)
            current = service.capture(user_id, domain=domain, target=target)
            if current != snapshot:
                raise ApiError('PREVIEW_STALE', '계획을 계산하는 동안 입력이 바뀌었어요. 다시 요청해 주세요', 409)
            ready = _fresh_ready(service, user_id, snapshot)
            if ready is not None:
                return ready
            response, placements = _present(conn, user_id, display_request, result, saved)
            timezone_name = conn.scalar(select(t.users.c.timezone).where(t.users.c.id == user_id))
            activate = target['draftSubjectIds'] if existing else [subject.id for subject in request.subjects]
            return service.create(user_id, domain=domain, target=target, snapshot=snapshot,
                normalized_input={'target': target, 'basis': snapshot.basis.dependency_versions},
                changes={'schemaVersion': 'plan-placements-v1', 'policy': POLICY_VERSION, 'timezone': timezone_name,
                    'activateSubjectIds': activate, 'placements': placements, 'supersedeIds': supersede},
                preview_response=response, proposal_type='initial', description='자료·목표·일정에 따른 초기 계획')

    def approve(self, user_id, preview_id):
        with self.engine.begin() as conn:
            try:
                candidate = ProposalRepository(conn).get(user_id, preview_id)
            except ProposalNotFound as error:
                raise ApiError('NO_PREVIEW', '미리보기를 찾지 못했어요', 404 if error.foreign else 400) from None
            except (LegacyProposalUnavailable, LegacyResponseUnavailable):
                raise ApiError('PREVIEW_STALE', '과거 후보를 확인할 수 없어요. 새 후보를 요청해 주세요', 409) from None
            from app.services.plan_resplit import approval_candidate
            candidate = approval_candidate(conn, user_id, candidate)
            preview_id = candidate['proposal_id']
            domains = {INITIAL_DOMAIN.kind: INITIAL_DOMAIN, ADDITION_DOMAIN.kind: ADDITION_DOMAIN}
            if candidate['kind'] not in domains:
                raise ApiError('NO_PREVIEW', '초기 계획 또는 과목 추가 후보가 아니에요', 404)
            return ProposalService(conn).apply(user_id, preview_id, domain=domains[candidate['kind']], target=candidate['target'])
