"""Immutable subject-setting candidates with real future allocations."""
from collections import defaultdict
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.scheduling import POLICY_VERSION, SearchLimit
from app.services.allocator import allocate
from app.services.plan_draft import _apply as apply_placements, _basis, _present
from app.services.plan_inputs import PlanInputError, PendingObligations, existing_allocation_input
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from app.services.subjects import SubjectService

FIELDS = {'examDate': 'exam_date', 'goal': 'target_grade', 'target': 'target_score'}
LABELS = {'examDate': '시험일', 'goal': '목표', 'target': '목표 점수'}


def _apply(conn, context, changes):
    values = dict(changes['values'])
    if 'exam_date' in values:
        values['exam_date'] = date.fromisoformat(values['exam_date'])
        values['onboarding_exam_date_confirmed_at'] = context.now
    if 'target_grade' in values or 'target_score' in values:
        values['onboarding_goal_confirmed_at'] = context.now
        values['goal_confirmed_at'] = context.now
    conn.execute(update(t.subjects).where(t.subjects.c.id == changes['subjectId'],
        t.subjects.c.user_id == context.user.id).values(**values, updated_at=context.now))
    apply_placements(conn, context, changes['allocation'])
    return next(card for card in SubjectService(conn, context.user.to_mapping()).listing()['subjects']
                if card['id'] == changes['subjectId'])


DOMAIN = PreviewDomain('subject_settings', PlanEffect.UPDATE, _basis, _apply)


def _subject(conn, user_id, subject_id):
    row = conn.execute(select(t.subjects).where(t.subjects.c.user_id == user_id,
        t.subjects.c.id == subject_id)).mappings().first()
    if row is None:
        raise ApiError('NO_SUBJECT', '과목을 찾지 못했어요', 404)
    if row['status'] != 'active':
        raise ApiError('INVALID_INPUT', '현재 계획에 포함된 과목만 변경할 수 있어요', 409)
    return dict(row)


def _ready(service, user_id, snapshot, normalized):
    for candidate in service.repository.ready(user_id, kind=DOMAIN.kind):
        if candidate['target'] != snapshot.target or candidate['normalized_input'] != normalized:
            continue
        try:
            service._require_fresh(candidate, snapshot)
        except ApiError:
            continue
        return candidate['preview_response']
    return None


class SubjectChangeService:
    def __init__(self, engine):
        self.engine = engine

    def preview(self, user_id, subject_id, body):
        target = {'subjectId': subject_id}
        normalized = body.model_dump(mode='json', exclude_unset=True)
        with self.engine.begin() as conn:
            row = _subject(conn, user_id, subject_id)
            if conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id == user_id)) is None:
                raise ApiError('NO_PLAN', '변경할 계획이 없어요', 400)
            service = ProposalService(conn)
            snapshot = service.capture(user_id, domain=DOMAIN, target=target)
            row = _subject(conn, user_id, subject_id)
            if body.examDate is not None and body.examDate <= snapshot.base_date:
                raise ApiError('INVALID_INPUT', '시험일은 오늘보다 뒤여야 해요', 422)
            values = {FIELDS[key]: value for key, value in normalized.items()
                      if (row[FIELDS[key]].isoformat() if isinstance(row[FIELDS[key]], date) else row[FIELDS[key]]) != value}
            if not values:
                raise ApiError('NO_CHANGE', '변경된 내용이 없어요', 400)
            ready = _ready(service, user_id, snapshot, normalized)
            if ready is not None:
                return ready
            timezone_name = conn.scalar(select(t.users.c.timezone).where(t.users.c.id == user_id))
            if 'target_grade' in values:
                values['raw_goal'] = values['target_grade']
            overrides = {key:value for key,value in values.items() if key != 'raw_goal'}
            if 'exam_date' in overrides:
                overrides['exam_date'] = date.fromisoformat(overrides['exam_date'])
            effective = snapshot.base_date + timedelta(days=1) if {'target_score','target_grade'} & values.keys() else snapshot.base_date
            try:
                request, supersede, _ = existing_allocation_input(conn, user_id, today=snapshot.base_date,
                    timezone_name=timezone_name, subject_ids=(), affected_subject_ids=(subject_id,),
                    subject_overrides={subject_id: overrides}, earliest_day=effective)
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
            current = service.capture(user_id, domain=DOMAIN, target=target)
            if current != snapshot:
                raise ApiError('PREVIEW_STALE', '계산 중 기준이 바뀌었어요', 409)
            ready = _ready(service, user_id, snapshot, normalized)
            if ready is not None:
                return ready
            _, placements = _present(conn, user_id, request, result, False)
            grouped = defaultdict(lambda: [0,0])
            for placement in placements:
                grouped[placement['date']][0] += 1
                grouped[placement['date']][1] += placement['minutes']
            response = {'subjectName': row['name'], 'changes': [
                {'label':LABELS[key], 'before':str(row[FIELDS[key]]), 'after':str(value)}
                for key,value in normalized.items() if FIELDS[key] in values],
                'days':[{'date':day, 'text':f'{count}개 태스크 · {minutes}분'} for day,(count,minutes) in sorted(grouped.items())],
                'note':'목표 가중치는 다음 날부터 반영해요' if effective > snapshot.base_date else None}
            return service.create(user_id, domain=DOMAIN, target=target, snapshot=snapshot,
                normalized_input=normalized, changes={'subjectId':subject_id, 'values':values,
                    'goalEffectiveDate':effective.isoformat(), 'previousTargetScore':row['target_score'],
                    'allocation':{'schemaVersion':'plan-placements-v1', 'policy':POLICY_VERSION, 'timezone':timezone_name,
                        'activateSubjectIds':[], 'placements':placements, 'supersedeIds':supersede}},
                preview_response=response, proposal_type='subject', description='시험일·목표와 남은 공부 계획 변경')

    def apply(self, user_id, subject_id, preview_id):
        with self.engine.begin() as conn:
            return ProposalService(conn).apply(user_id, preview_id, domain=DOMAIN, target={'subjectId':subject_id})
