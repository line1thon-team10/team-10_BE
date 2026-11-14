"""Durable A/B/C candidates: capture, calculate outside the transaction, recapture."""
from dataclasses import replace
from datetime import date
from uuid import uuid4

from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import PageSpan, intersect
from app.domain.scheduling import POLICY_VERSION, SearchLimit
from app.repositories.proposals import ProposalRepository, ProposalNotFound, LegacyProposalUnavailable, canonical_hash
from app.services.plan_draft import _basis, _apply, _present, _fresh_ready, _json_value
from app.services.plan_inputs import existing_allocation_input, PlanInputError
from app.services.proposals import DomainBasis, PlanEffect, PreviewDomain, ProposalService
from app.services.replan_candidates import build_recovery_candidates, RecoveryCandidate
from app.services.study import StudyData

KEEP_NOTE = '보충 학습은 어느 안에서도 빼거나 줄이지 않아요'


def _pending(conn, user_id, identifier=None):
    query = select(t.plan_change_requests).where(t.plan_change_requests.c.user_id == user_id,
        t.plan_change_requests.c.kind == 'recovery')
    if identifier is not None:
        query = query.where(t.plan_change_requests.c.id == identifier)
    else:
        query = query.where(t.plan_change_requests.c.status == 'ready')
    row = conn.execute(query.order_by(t.plan_change_requests.c.created_at.desc())).mappings().first()
    return dict(row) if row else None


def _recovery_basis(conn, context, target):
    request = _pending(conn, context.user.id, target['recoveryRequestId'])
    if request is None or request['status'] != 'ready':
        raise ApiError('PREVIEW_STALE', '밀림 판정이 바뀌었어요. 새 후보를 확인해 주세요', 409)
    basis = _basis(conn, context, target)
    return DomainBasis({**basis.dependency_versions, 'recovery': canonical_hash(_json_value(request))},
                       structure_hash=basis.structure_hash)


def _apply_recovery(conn, context, changes, key):
    selected = changes['options'][key]
    if not selected['enabled']:
        raise ApiError('BAD_KEY', '실행 가능한 분배안을 선택해 주세요', 400)
    for target in selected['scopeExclusions']:
        table = t.concepts if target['kind'] == 'concept' else t.units
        conn.execute(update(table).where(table.c.id == target['id']).values(in_scope=False))
    _apply(conn, context, selected['allocation'])
    conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == changes['recoveryRequestId'],
        t.plan_change_requests.c.user_id == context.user.id, t.plan_change_requests.c.status == 'ready').values(
            status='accepted', accepted_proposal_id=changes['previewId'], decided_at=context.now, updated_at=context.now))
    conn.execute(t.user_notices.insert().values(id=str(uuid4()),user_id=context.user.id,
        dedupe_key='recovery:'+changes['previewId'],notice_type='recovery',notice_date=context.today,
        status='pending',payload={'key':key,'previewId':changes['previewId']},created_at=context.now))
    return {'event': {'type':'box','reason':'recover','planKey':key}}


def domain(key='A'):
    return PreviewDomain('recovery_options',PlanEffect.UPDATE,_recovery_basis,
                         lambda conn,context,changes: _apply_recovery(conn,context,changes,key))


def _excludable(conn, request):
    units={row['id']:dict(row) for row in conn.execute(select(t.units).where(t.units.c.id.in_([u.id for u in request.units]))).mappings()}
    spans,targets=[],{}
    for unit in sorted(request.units,key=lambda u:(float(units[u.id]['emphasis_weight'] or 1),-u.sort_order,u.id)):
        row=units[unit.id]
        if float(row['emphasis_weight'] or 1)>1 or not unit.ranges:
            continue
        concepts=list(conn.execute(select(t.concepts).where(t.concepts.c.unit_id==unit.id,t.concepts.c.in_scope.is_(True),
            t.concepts.c.retired_at.is_(None)).order_by(t.concepts.c.sort_order.desc(),t.concepts.c.id)).mappings())
        if not concepts:
            if len(unit.ranges)==1 and unit.ranges[0].start==row['start_page'] and unit.ranges[0].end==row['end_page']:
                span=unit.ranges[0]; spans.append(span); targets[span]={'kind':'unit','id':unit.id}
            continue
        for concept in concepts:
            span=PageSpan(unit.material_id,concept['start_page'],concept['end_page'])
            # Overlapping selected concepts cannot be physically removed by
            # toggling only one concept; leave them for an explicit scope edit.
            others=[PageSpan(unit.material_id,c['start_page'],c['end_page']) for c in concepts if c['id']!=concept['id']]
            if intersect([span],others):
                continue
            spans.append(span);targets[span]={'kind':'concept','id':concept['id']}
    return tuple(spans),targets


class ReplanOptionsService:
    def __init__(self,engine):
        self.engine=engine

    def options(self,user_id,*,saved=False):
        with self.engine.begin() as conn:
            pending=_pending(conn,user_id)
            if pending is None:
                raise ApiError('NO_REPLAN','지금은 밀린 분량이 없어요',404)
            target={'recoveryRequestId':pending['id']}
            service=ProposalService(conn)
            snapshot=service.capture(user_id,domain=domain(),target=target)
            ready=_fresh_ready(service,user_id,snapshot)
            if ready is not None:
                return {**ready,'saved':saved}
            timezone_name=conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id))
            data=StudyData(conn,user_id)
            movable=tuple(task.id for task in data.tasks if task.row['status'] in ('planned','partial','missed')
                          and task.row['subject_id'] in data.subject_by_id and task.row['task_type'] in ('remedial','review'))
            preparation_error=None
            try:
                request,supersede,display=existing_allocation_input(conn,user_id,today=snapshot.base_date,
                    timezone_name=timezone_name,subject_ids=(),material_structure_overrides={},movable_task_ids=movable)
            except PlanInputError as error:
                preparation_error=str(error)
                request,supersede=None,[]
            excludable,exclusion_targets=_excludable(conn,request) if request is not None else ((),{})
        try:
            candidates=({key:RecoveryCandidate(False,None,None,(),preparation_error) for key in ('A','B','C')}
                        if preparation_error else build_recovery_candidates(request,excludable_ranges=excludable))
        except SearchLimit:
            raise ApiError('PLAN_SEARCH_LIMIT','계획 탐색 한도에 도달했어요. 다시 시도해 주세요',503) from None
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            current=service.capture(user_id,domain=domain(),target=target)
            if current!=snapshot:
                raise ApiError('PREVIEW_STALE','계획을 계산하는 동안 입력이 바뀌었어요',409)
            ready=_fresh_ready(service,user_id,snapshot)
            if ready is not None:
                return {**ready,'saved':saved}
            identifier=str(uuid4())
            stored,views={},[]
            labels={'A':'가용 시간에 추가 배정','B':'미학습·비강조 범위 제외','C':'남은 새 학습을 30% 압축'}
            for key,candidate in candidates.items():
                placements=[]
                if candidate.enabled:
                    _,placements=_present(conn,user_id,candidate.effective_request,candidate.result,False)
                allocation={'schemaVersion':'plan-placements-v1','policy':POLICY_VERSION,'timezone':timezone_name,
                    'placements':placements,'supersedeIds':supersede,'activateSubjectIds':[]}
                stored[key]={'enabled':candidate.enabled,'allocation':allocation,
                    'scopeExclusions':[exclusion_targets[span] for span in candidate.exclusions]}
                total=sum(item['minutes'] for item in placements)
                views.append({'key':key,'enabled':candidate.enabled,'lines':[labels[key],candidate.reason],
                    'effect':f'새 배치 {len(placements)}개 · {total}분' if candidate.enabled else '현재 조건에서 실행할 수 없어요',
                    'preview':candidate.reason,'totalMinutes':total})
            recovery=pending['recovery_context'] or {}
            overdue=[{'subjectId':s['subjectId'],'pages':s['overduePages'],'dailyAvg':round(s['dailyAveragePages'])}
                     for s in recovery.get('subjects',[])]
            response={'kind':recovery.get('reason','overdue'),'saved':saved,'title':None,'date':snapshot.base_date.isoformat(),
                'totalPages':sum(row['pages'] for row in overdue),'overdue':overdue,'keepNote':KEEP_NOTE,'options':views}
            return service.create(user_id,domain=domain(),target=target,snapshot=snapshot,
                normalized_input={'recoveryRequestId':pending['id'],'basis':snapshot.basis.dependency_versions},
                changes={'schemaVersion':'recovery-options-v1','previewId':identifier,'recoveryRequestId':pending['id'],'options':stored},
                preview_response=response,preview_id=identifier,proposal_type='recovery',description='실제 가용 시간으로 계산한 A/B/C')

    def apply(self,user_id,preview_id,key):
        if key not in ('A','B','C'):
            raise ApiError('BAD_KEY','분배안을 다시 골라 주세요',400)
        with self.engine.begin() as conn:
            try:
                state=ProposalRepository(conn).get(user_id,str(preview_id))
            except ProposalNotFound:
                raise ApiError('NO_PREVIEW','미리보기를 찾지 못했어요',404) from None
            except LegacyProposalUnavailable:
                raise ApiError('PREVIEW_STALE','과거 후보를 다시 계산해 주세요',409) from None
            if state['kind']!='recovery_options':
                raise ApiError('NO_PREVIEW','이 작업의 미리보기가 아니에요',404)
            result=ProposalService(conn).apply(user_id,str(preview_id),domain=domain(key),target=state['target'])
            if result['event']['planKey']!=key:
                raise ApiError('PREVIEW_STALE','이 후보는 다른 안으로 이미 적용했어요',409)
            return result
