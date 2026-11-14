"""Durable request dialogue and immutable remaining-plan replacement candidates."""
from collections import defaultdict
from dataclasses import replace
from datetime import date
from uuid import uuid4

from sqlalchemy import select, update

from app.ai.gateway import CleanupFailed
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import subtract
from app.domain.scheduling import POLICY_VERSION, SearchLimit, StudyWindow, half_up
from app.repositories.proposals import ProposalRepository, ProposalNotFound, LegacyProposalUnavailable, canonical_hash
from app.services.allocator import allocate
from app.services.plan_draft import PlanDraftService, INITIAL_DOMAIN, _apply, _basis, _present
from app.services.plan_inputs import initial_allocation_input, existing_allocation_input, PlanInputError, PendingObligations
from app.services.proposals import PreviewDomain, ProposalService, PlanEffect


def _failed():
    return ApiError('AI_FAILED', '요청에 맞는 재분할 해석을 준비하지 못했어요. 다시 요청해 주세요', 503)


def _update_plan(conn, context, changes):
    _apply(conn, context, changes['allocation'])
    return {'applied': True}


def _replace_draft(conn, context, changes):
    """Select an initial draft only; plan/task creation belongs to /approve."""
    service = ProposalService(conn)
    target = changes['initialTarget']
    for row in service.repository.ready(context.user.id, kind=INITIAL_DOMAIN.kind):
        if row['target'] == target:
            service.repository.cancel(context.user.id, row['proposal_id'], expected_revision=row['state_revision'], now=context.now)
    snapshot = service.capture(context.user.id, domain=INITIAL_DOMAIN, target=target)
    service.create(context.user.id, domain=INITIAL_DOMAIN, target=target, snapshot=snapshot,
        normalized_input={'target':target, 'basis':snapshot.basis.dependency_versions},
        changes=changes['allocation'], preview_response=changes['draftResponse'],
        preview_id=changes['replacementPreviewId'], proposal_type='initial', description='승인 전 다시 나눈 초기 계획')
    return {'applied': True}


DRAFT_DOMAIN = PreviewDomain('initial_plan_resplit', PlanEffect.DRAFT, _basis, _replace_draft)
ACTIVE_DOMAIN = PreviewDomain('remaining_plan_resplit', PlanEffect.UPDATE, _basis, _update_plan)
DOMAINS = {domain.kind:domain for domain in (DRAFT_DOMAIN, ACTIVE_DOMAIN)}


def approval_candidate(conn, user_id, candidate):
    """/approve explicitly authorizes both draft selection and first approval."""
    if candidate['kind'] != DRAFT_DOMAIN.kind:
        return candidate
    ProposalService(conn).apply(user_id, candidate['proposal_id'], domain=DRAFT_DOMAIN, target=candidate['target'])
    return ProposalRepository(conn).get(user_id, candidate['changes']['replacementPreviewId'])


def _snapshot_value(snapshot):
    return {'day':snapshot.base_date.isoformat(), 'planId':snapshot.base_plan_id,
            'revision':snapshot.base_plan_revision, 'basis':snapshot.basis.dependency_versions,
            'structureHash':snapshot.basis.structure_hash}


def _compare(conn, user_id, request, result, before, text, saved):
    old = defaultdict(int)
    for task in before:
        old[task['date']] += task['minutes']
    new = defaultdict(int)
    for task in (*result.placements, *result.fixed):
        if task.day >= request.start_date:
            new[task.day.isoformat()] += half_up(task.minutes)
    dates = sorted(set(old) | set(new))
    return {'request':text, 'keep':'학습 기록·점수·책·시작한 태스크와 보충·복습 의무를 유지해요', 'saved':saved,
        'days':[{'date':day, 'before':old[day], 'after':new[day], 'changed':old[day]!=new[day]} for day in dates],
        'stats':[{'label':'총 공부 시간', 'unit':'분', 'before':sum(old.values()), 'after':sum(new.values())},
                 {'label':'태스크', 'unit':'개', 'before':len(before),
                  'after':len(result.placements)+sum(task.day>=request.start_date for task in result.fixed)}],
        'reasons':[reason.message for reason in result.reasons]}


class PlanResplitService:
    def __init__(self, engine, *, interpreter=None):
        self.engine = engine
        self.interpreter = interpreter

    def _interpret(self, user_id, payload, saved):
        if self.interpreter is not None:
            return self.interpreter(user_id, payload, saved)
        # The adapter owns bounded external interpretation; allocation remains
        # deterministic and never trusts AI-proposed placements or page totals.
        from app.ai.gateway import AiGateway, GatewayRequest, ResultIdentity
        from app.ai.resplit_adapter import PROVIDER, VALIDATOR, MODEL_VERSION, PROMPT_VERSION, SCHEMA_VERSION
        identity=ResultIdentity.create(kind='plan_redo',provider='groq',model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION,schema_version=SCHEMA_VERSION,input=payload,validation_context=payload)
        outcome=AiGateway(self.engine).run(GatewayRequest(user_id,identity),validator=VALIDATOR,
            provider=None if saved else PROVIDER,mode='backup' if saved else 'live')
        return outcome.data

    def _initial(self, user_id):
        selected = None
        with self.engine.begin() as conn:
            service = ProposalService(conn)
            if conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id==user_id)):
                return None
            candidates = service.repository.ready(user_id, kind=INITIAL_DOMAIN.kind)
            for candidate in reversed(candidates):
                if selected is None:
                    selected = candidate['target']
                snapshot = service.capture(user_id, domain=INITIAL_DOMAIN, target=candidate['target'])
                try:
                    service._require_fresh(candidate, snapshot)
                except ApiError:
                    continue
                return candidate['preview_response']
        # A stale draft still identifies the user's selected subjects. Never
        # broaden it to every draft subject while regenerating its basis.
        identifiers = None if selected is None else selected.get('draftSubjectIds')
        return PlanDraftService(self.engine).draft(user_id,
            draft_subject_ids=None if identifiers is None else tuple(identifiers))

    def _candidate(self, conn, user_id, identifier=None):
        repo=ProposalRepository(conn)
        if identifier is None:
            rows=[row for kind in DOMAINS for row in repo.ready(user_id,kind=kind)]
            if len(rows)>1:
                raise ApiError('PREVIEW_REQUIRED','비교할 previewId를 지정해 주세요',409)
            if not rows:
                raise ApiError('NO_PREVIEW','비교할 재분할 후보가 없어요',400)
            return rows[0]
        try:
            row=repo.get(user_id,identifier)
        except ProposalNotFound as exc:
            raise ApiError('NO_PREVIEW','재분할 후보를 찾지 못했어요',404 if exc.foreign else 400) from None
        except LegacyProposalUnavailable:
            raise ApiError('PREVIEW_STALE','과거 후보는 새로 요청해 주세요',409) from None
        if row['kind'] not in DOMAINS:
            raise ApiError('NO_PREVIEW','재분할 후보가 아니에요',404)
        return row

    def view(self, user_id, preview_id=None):
        with self.engine.begin() as conn:
            row=self._candidate(conn,user_id,preview_id)
            if row['status'] not in ('ready','applied'):
                raise ApiError('PREVIEW_STALE','취소되거나 만료된 후보예요',409)
            if row['status']=='ready':
                service=ProposalService(conn)
                service._require_fresh(row,service.capture(user_id,domain=DOMAINS[row['kind']],target=row['target']))
            return row['preview_response']

    def apply(self,user_id,preview_id):
        with self.engine.begin() as conn:
            row=self._candidate(conn,user_id,preview_id)
            return ProposalService(conn).apply(user_id,preview_id,domain=DOMAINS[row['kind']],target=row['target'])

    def cancel(self,user_id,preview_id):
        with self.engine.begin() as conn:
            row=self._candidate(conn,user_id,preview_id)
            return ProposalService(conn).cancel(user_id,preview_id,domain=DOMAINS[row['kind']],target=row['target'])

    def prepare(self,user_id,text,*,answer=None,request_id=None,saved=False,deterministic=False):
        supplied_request_id=request_id
        initial=self._initial(user_id)
        target={'scope':'remaining'} if initial is None else {'scope':'initial','sourcePreviewId':initial['previewId']}
        domain=ACTIVE_DOMAIN if initial is None else DRAFT_DOMAIN
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            snapshot=service.capture(user_id,domain=domain,target=target)
            basis=_snapshot_value(snapshot)
            if request_id:
                request_row=conn.execute(select(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id,
                    t.plan_change_requests.c.user_id==user_id)).mappings().first()
                if request_row is None or request_row['kind']!=domain.kind or request_row['request_text']!=text:
                    raise ApiError('INVALID_INPUT','원래 요청과 requestId가 일치해야 해요',422)
                envelope=request_row['input_payload']
                if envelope.get('basis')!=basis:
                    raise ApiError('PREVIEW_STALE','질문 이후 계획 기준이 바뀌었어요',409)
                if request_row['followup_answer'] is not None and request_row['followup_answer']!=answer:
                    raise ApiError('INVALID_INPUT','이미 답한 후속 질문을 바꿀 수 없어요',422)
                if answer is not None and request_row['followup_question'] is None:
                    raise ApiError('INVALID_INPUT','후속 질문이 없는 요청이에요',422)
                existing=conn.execute(select(t.plan_proposal_states.c.proposal_id).where(
                    t.plan_proposal_states.c.user_id==user_id).join(t.plan_proposals,
                    t.plan_proposals.c.id==t.plan_proposal_states.c.proposal_id).where(t.plan_proposals.c.request_id==request_id)).scalar()
                if existing:
                    row=self._candidate(conn,user_id,existing)
                    if row['status'] not in ('ready','applied'):
                        raise ApiError('PREVIEW_STALE','취소된 요청은 새로 요청해 주세요',409)
                    return {'ready':True,'question':None,'previewId':existing,'requestId':request_id}
                if request_row['status']=='question' and answer is None:
                    return envelope['questionResponse']
                if request_row['status']=='processing':
                    raise ApiError('REQUEST_IN_PROGRESS','같은 요청을 처리 중이에요',409)
                conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id).values(
                    status='processing',followup_answer=answer,updated_at=now_utc()))
            else:
                for row in service.repository.ready(user_id,kind=domain.kind):
                    if row['normalized_input'].get('text')==text and row['normalized_input'].get('answer') is None:
                        try:
                            service._require_fresh(row,snapshot)
                        except ApiError:
                            continue
                        return {'ready':True,'question':None,'previewId':row['proposal_id'],'requestId':row['request_id']}
                request_id=str(uuid4())
                conn.execute(t.plan_change_requests.insert().values(id=request_id,user_id=user_id,kind=domain.kind,
                    status='processing',base_plan_id=snapshot.base_plan_id,base_plan_revision=snapshot.base_plan_revision,
                    request_text=text,input_payload={'schemaVersion':'plan-resplit-request-v1','basis':basis},
                    created_at=now_utc(),updated_at=now_utc()))
            timezone_name=conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id))
            source=None if initial is None else service.repository.get(user_id,initial['previewId'])
            try:
                if initial is None:
                    subjects=conn.scalars(select(t.subjects.c.id).where(t.subjects.c.user_id==user_id,t.subjects.c.status=='active')).all()
                    allocation,supersede,display=existing_allocation_input(conn,user_id,today=snapshot.base_date,
                        timezone_name=timezone_name,subject_ids=(),affected_subject_ids=subjects)
                    before=[{'date':row['scheduled_date'].isoformat(),'minutes':row['estimated_minutes']}
                        for row in conn.execute(select(t.study_tasks).join(t.subjects,t.subjects.c.id==t.study_tasks.c.subject_id)
                            .where(t.subjects.c.user_id==user_id,t.study_tasks.c.plan_id==snapshot.base_plan_id,
                                t.study_tasks.c.status.not_in(('carried_over','excluded')),t.study_tasks.c.scheduled_date>=snapshot.base_date)).mappings()]
                else:
                    allocation=initial_allocation_input(conn,user_id,today=snapshot.base_date,timezone_name=timezone_name,
                        subject_ids=None if source['target'].get('draftSubjectIds') is None else tuple(source['target']['draftSubjectIds']))
                    supersede=[]; display=allocation; before=source['changes']['placements']
            except PendingObligations as exc:
                raise ApiError('OVERDUE_OBLIGATIONS',str(exc),409) from None
            except PlanInputError as exc:
                raise ApiError('INVALID_INPUT',str(exc),422) from None
            subject_rows=conn.execute(select(t.subjects).where(t.subjects.c.id.in_([sub.id for sub in allocation.subjects])).order_by(t.subjects.c.id)).mappings()
            payload={'request':text,'answer':answer,'today':snapshot.base_date.isoformat(),
                'subjects':[{'id':row['id'],'name':row['name'],'examDate':row['exam_date'].isoformat()} for row in subject_rows],
                'basisHash':canonical_hash(basis)}
        try:
            intent={'ready':True,'question':None,'windows':[]} if deterministic and not saved else self._interpret(user_id,payload,saved)
            if not intent['ready']:
                if answer is not None:
                    raise _failed()
                response={'ready':False,'question':intent['question'],'requestId':request_id}
                with self.engine.begin() as conn:
                    current=ProposalService(conn).capture(user_id,domain=domain,target=target)
                    if current!=snapshot:
                        raise ApiError('PREVIEW_STALE','해석하는 동안 계획 기준이 바뀌었어요',409)
                    row=conn.execute(select(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id)).mappings().one()
                    conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id,
                        t.plan_change_requests.c.status=='processing').values(status='question',followup_question=intent['question']['text'],
                        input_payload={**row['input_payload'],'questionResponse':response},updated_at=now_utc()))
                return response
            windows=[]
            fixed=tuple(part.span for task in allocation.fixed if task.kind=='study' for part in task.ranges)
            for index,window in enumerate(intent['windows']):
                spans=tuple(span for unit in allocation.units if window['subjectId'] is None or unit.subject_id==window['subjectId'] for span in unit.ranges)
                windows.append(StudyWindow('resplit:'+str(index),subtract(spans,fixed),
                    tuple(date.fromisoformat(day) for day in window['dates']),window['minutesPerDay']))
            allocation=replace(allocation,study_windows=tuple(windows))
            result=allocate(allocation)
            if not result.feasible:
                raise ApiError('PLAN_INFEASIBLE',' '.join(reason.message for reason in result.reasons),409)
            with self.engine.begin() as conn:
                service=ProposalService(conn)
                if service.capture(user_id,domain=domain,target=target)!=snapshot:
                    raise ApiError('PREVIEW_STALE','계산하는 동안 계획 기준이 바뀌었어요',409)
                if supplied_request_id is None:
                    for existing in service.repository.ready(user_id,kind=domain.kind):
                        normalized=existing['normalized_input']
                        if normalized.get('text')==text and normalized.get('answer')==answer and normalized.get('intent')==intent:
                            try:
                                service._require_fresh(existing,snapshot)
                            except ApiError:
                                continue
                            conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id).values(
                                status='cancelled',updated_at=now_utc(),decided_at=now_utc()))
                            return {'ready':True,'question':None,'previewId':existing['proposal_id'],'requestId':existing['request_id']}
                row=conn.execute(select(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id)).mappings().one()
                if row['status']!='processing':
                    raise ApiError('PREVIEW_STALE','요청 상태가 바뀌었어요',409)
                draft_response,placements=_present(conn,user_id,display,result,saved)
                changes={'allocation':{'schemaVersion':'plan-placements-v1','policy':POLICY_VERSION,'timezone':timezone_name,
                    'activateSubjectIds':[] if source is None else source['changes']['activateSubjectIds'],
                    'placements':placements,'supersedeIds':supersede}}
                if source is not None:
                    changes.update(initialTarget=source['target'],draftResponse=draft_response,replacementPreviewId=str(uuid4()))
                response=_compare(conn,user_id,allocation,result,before,text,saved)
                response['requestId']=request_id
                conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id).values(status='ready',updated_at=now_utc()))
                stored=service.create(user_id,domain=domain,target=target,snapshot=snapshot,
                    normalized_input={'text':text,'answer':answer,'intent':intent,'basis':basis},changes=changes,
                    preview_response=response,request_id=request_id,request_text=text,
                    proposal_type='resplit',description='기록과 의무를 보존한 계획 재분할')
                return {'ready':True,'question':None,'previewId':stored['previewId'],'requestId':request_id}
        except Exception as exc:
            with self.engine.begin() as conn:
                conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id==request_id,
                    t.plan_change_requests.c.user_id==user_id,t.plan_change_requests.c.status=='processing').values(status='failed',updated_at=now_utc()))
            if isinstance(exc,CleanupFailed):
                raise
            if isinstance(exc,SearchLimit):
                raise ApiError('PLAN_SEARCH_LIMIT','계획 탐색 한도에 도달했어요',503) from None
            if isinstance(exc,ApiError):
                raise
            raise _failed() from None
