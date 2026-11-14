"""Owned range additions are immutable previews, never immediate task writes."""
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import PageSpan, intersect, page_count, subtract
from app.domain.scheduling import POLICY_VERSION, MULTIPLIERS, StudyWindow, SearchLimit, half_up
from app.repositories.proposals import ProposalRepository, ProposalNotFound, LegacyProposalUnavailable, LegacyResponseUnavailable
from app.schemas.units import UnitsEdit
from app.services.allocator import allocate
from app.services.material_structure import _placements
from app.services.plan_draft import _apply as apply_placements, _basis
from app.services.plan_inputs import existing_allocation_input, PlanInputError, PendingObligations
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from app.services.unit_edits import prepare_structure, persist_structure
from app.services.units import UnitService


def _apply(conn, context, changes):
    if not changes['feasible']:
        raise ApiError('PLAN_INFEASIBLE', '충돌이나 미배정 페이지가 있어 적용할 수 없어요', 409)
    persist_structure(conn, context.user.id, changes['structure'], context.now)
    apply_placements(conn, context, changes['allocation'])
    return {'applied': True}


DOMAIN = PreviewDomain('subject_range_addition', PlanEffect.UPDATE, _basis, _apply)


def _ready(service, user_id, snapshot, normalized):
    for row in service.repository.ready(user_id, kind=DOMAIN.kind):
        if row['target'] != snapshot.target or row['normalized_input'] != normalized:
            continue
        try:
            service._require_fresh(row, snapshot)
        except ApiError:
            continue
        return row['preview_response']
    return None


def _expand_structure(conn, user_id, body, selected):
    structure = UnitService(conn).get(user_id, body.subjectId, body.materialId)
    covered = []
    used_numbers = {unit['no'] for unit in structure['units']}
    maximum = max((number or 0 for number in used_numbers), default=0)
    for unit in structure['units']:
        unit.pop('quizless')
        unit.pop('evidence')
        span = PageSpan(body.materialId, unit['from'], unit['to'])
        covered.append(span)
        requested = intersect((span,), selected)
        if not requested:
            continue
        included = ([PageSpan(body.materialId, c['from'], c['to']) for c in unit['concepts'] if c['inExam']]
            if unit['concepts'] else [span]) if unit['inExam'] else []
        missing = subtract(requested, included)
        if not missing:
            continue
        if not unit['inExam']:
            for child in unit['concepts']:
                child['inExam'] = False
        unit['inExam'] = True
        if not unit['concepts'] and missing == (span,):
            continue
        unit['concepts'].extend({'title': f'추가 범위 {part.start}–{part.end}쪽',
            'from':part.start, 'to':part.end, 'inExam':True} for part in missing)
    for part in subtract(selected, covered):
        maximum += 1
        if maximum > 2_147_483_647:
            maximum = 1
        while maximum in used_numbers:
            maximum = maximum+1 if maximum < 2_147_483_647 else 1
        used_numbers.add(maximum)
        structure['units'].append({'no':maximum, 'title':f'추가 범위 {part.start}–{part.end}쪽',
            'from':part.start, 'to':part.end, 'difficulty':None, 'inExam':True, 'concepts':[]})
    structure.pop('file')
    return prepare_structure(conn, user_id, UnitsEdit.model_validate(structure))


def _clock(minute):
    # Match the immutable placement timestamp's microsecond rounding before
    # producing the shared HH:mm display; exact values remain in changes.
    total = int(minute)
    return f'{total//60:02d}:{total%60:02d}'


def _preview_pieces(result, selected):
    pieces = []
    for task in result.placements:
        cursor = task.start
        for part in task.parts:
            cost = part.baseline_minutes_per_page * part.difficulty_multiplier
            for span in intersect((part.span,), selected):
                first = cursor + (span.start-part.span.start)*cost
                last = first+(span.end-span.start+1)*cost
                ranges = [{'materialId':span.material_id, 'unitId':part.unit_id, 'from':span.start, 'to':span.end}]
                pieces.append({'date':task.day.isoformat(), 'start':_clock(first), 'end':_clock(last),
                    'from':span.start, 'to':span.end, 'minutes':half_up(last-first),
                    'reason':'기존 미완료 범위와 함께 추가 목표를 배분했어요',
                    'materialId':span.material_id, 'unitId':part.unit_id, 'ranges':ranges,
                    'pages':span.end-span.start+1})
            cursor += part.minutes
    return pieces


class PlanAdditionsService:
    def __init__(self, engine):
        self.engine = engine

    def preview(self, user_id, body):
        normalized = body.model_dump(mode='json', by_alias=True)
        target = {'subjectId':body.subjectId, 'materialId':body.materialId}
        selected = (PageSpan(body.materialId,body.from_,body.from_+body.totalPages-1 if body.totalPages else body.to),)
        with self.engine.begin() as conn:
            service = ProposalService(conn)
            if conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id==user_id)) is None:
                raise ApiError('NO_PLAN','승인된 계획이 필요해요',400)
            snapshot = service.capture(user_id,domain=DOMAIN,target=target)
            subject = conn.execute(select(t.subjects).where(t.subjects.c.id==body.subjectId,
                t.subjects.c.user_id==user_id, t.subjects.c.status=='active')).mappings().first()
            material = conn.execute(select(t.materials).where(t.materials.c.id==body.materialId,
                t.materials.c.subject_id==body.subjectId)).mappings().first()
            if subject is None or material is None:
                raise ApiError('NOT_FOUND','본인의 활성 과목과 자료를 선택해 주세요',404)
            if (type(material['page_count']) is not int or not body.to<=material['page_count']
                    or subject['exam_date'] is None or not snapshot.base_date<=body.startDate<=body.endDate<subject['exam_date']):
                raise ApiError('INVALID_INPUT','실제 자료 범위와 시험 전 날짜를 선택해 주세요',422)
            ready = _ready(service,user_id,snapshot,normalized)
            if ready is not None:
                return ready
            candidate = _expand_structure(conn,user_id,body,selected)
            timezone_name = conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id))
            try:
                request,supersede,scope_request = existing_allocation_input(conn,user_id,today=snapshot.base_date,
                    timezone_name=timezone_name,subject_ids=(),affected_subject_ids=(body.subjectId,),
                    material_structure_overrides={body.materialId:{'units':candidate['units'],'concepts':candidate['concepts']}})
            except PendingObligations as exc:
                raise ApiError('OVERDUE_OBLIGATIONS',str(exc),409) from None
            except PlanInputError as exc:
                raise ApiError('INVALID_INPUT',str(exc),422) from None
            available = tuple(span for unit in request.units for span in unit.ranges)
            fixed = tuple(part.span for task in request.fixed if task.subject_id==body.subjectId and task.kind=='study'
                          for part in task.ranges)
            selected_pending = subtract(intersect(selected,available),fixed)
            dates = tuple(body.startDate+timedelta(days=i) for i in range((body.endDate-body.startDate).days+1)
                          if (body.startDate+timedelta(days=i)).isoweekday() in body.days)
            request = replace(request,study_windows=(StudyWindow('addition',selected_pending,dates,body.minutesPerDay),))
            summary = {'subjectName':subject['name'], 'materialName':material['original_name'],
                'from':body.from_, 'to':body.to, 'pages':page_count(selected), 'minutesPerDay':body.minutesPerDay,
                'totalPages':page_count(selected), 'goalType':'time' if body.minutesPerDay else 'pages',
                'totalMinutes':half_up(sum((part.end-part.start+1)*next(subject.minutes_per_page for subject in request.subjects
                    if subject.id==body.subjectId) * MULTIPLIERS[unit.difficulty]
                    for unit in scope_request.units for part in intersect(unit.ranges,selected))),
                'days':body.days,'startDate':body.startDate.isoformat(),'endDate':body.endDate.isoformat()}
        try:
            result=allocate(request)
        except SearchLimit:
            raise ApiError('PLAN_SEARCH_LIMIT','계획 탐색 한도에 도달했어요. 다시 요청해 주세요',503) from None
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            current=service.capture(user_id,domain=DOMAIN,target=target)
            if current!=snapshot:
                raise ApiError('PREVIEW_STALE','계산하는 동안 계획 기준이 바뀌었어요',409)
            ready=_ready(service,user_id,snapshot,normalized)
            if ready is not None:
                return ready
            placements=_placements(request,result,candidate,conn) if result.feasible else []
            pieces=_preview_pieces(result,selected_pending) if result.feasible else []
            conflicts=0 if result.feasible else max(1,len(result.reasons))
            response={'summary':summary,'placements':pieces,'conflicts':conflicts,
                'leftPages':0 if result.feasible else page_count(selected_pending),
                'note':' '.join([*[reason.message for reason in result.reasons],
                    *(['이미 학습했거나 시작한 범위는 기존 기록과 고정 배치로 충족하고 남은 부분만 추가 배분해요']
                      if page_count(selected_pending)<page_count(selected) else [])]) or None}
            return service.create(user_id,domain=DOMAIN,target=target,snapshot=snapshot,normalized_input=normalized,
                changes={'feasible':result.feasible,'structure':candidate,'allocation':{'schemaVersion':'plan-placements-v1',
                    'policy':POLICY_VERSION,'timezone':timezone_name,'activateSubjectIds':[],
                    'supersedeIds':supersede,'placements':placements}},preview_response=response,
                proposal_type='add_plan',description='기존 과목 범위와 기간별 목표 추가')

    def apply(self,user_id,preview_id):
        with self.engine.begin() as conn:
            try:
                candidate=ProposalRepository(conn).get(user_id,preview_id)
            except ProposalNotFound as exc:
                raise ApiError('NO_PREVIEW','미리보기를 찾지 못했어요',404 if exc.foreign else 400) from None
            except (LegacyProposalUnavailable,LegacyResponseUnavailable):
                raise ApiError('PREVIEW_STALE','과거 후보를 확인할 수 없어요',409) from None
            if candidate['kind']!=DOMAIN.kind:
                raise ApiError('NO_PREVIEW','범위 추가 후보가 아니에요',404)
            return ProposalService(conn).apply(user_id,preview_id,domain=DOMAIN,target=candidate['target'])
