"""Grounded language interpretation creates a schedule preview, never approval."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.ai.gateway import AiGateway, GatewayRequest, InvalidOutput, ResultIdentity
from app.ai.onboarding_adapter import MODEL_VERSION, PROMPT_VERSION, PROVIDER, SCHEMA_VERSION, VALIDATOR, validate_onboarding_result
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.onboarding_voice_evidence import validate_voice_evidence
from app.schemas.onboarding_events import EventCreate
from app.services.onboarding_events import _local_time_to_utc
from app.services.plan_draft import _basis, _json_value
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from app.services.schedule_changes import (_allocation,_after_occurrences,_edited,_extra_move_view,_move_view,
    _persist,_saved_allocation)
from app.services.allocator import allocate
from app.domain.scheduling import SearchLimit

VOICE_DOMAIN=PreviewDomain('schedule_voice',PlanEffect.UPDATE,_basis,_persist,after_apply=_edited)


class ScheduleVoiceService:
    def __init__(self,engine,*,gateway=None):
        self.engine=engine
        self.gateway=gateway or AiGateway(engine)

    def preview(self,user_id,text):
        target={'source':'voice_schedule'}
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            snapshot=service.capture(user_id,domain=VOICE_DOMAIN,target=target)
            user=dict(conn.execute(select(t.users).where(t.users.c.id==user_id)).mappings().one())
            context={'mode':'voice','text':text,'today':snapshot.base_date.isoformat(),'timezone':user['timezone'],
                'subjectId':None,'subjects':[],'state':{}}
        identity=ResultIdentity.create(kind='nl_parse',provider='groq',model_version=MODEL_VERSION,prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,input=context,validation_context=context)
        parsed=self.gateway.run(GatewayRequest(user_id,identity),validator=VALIDATOR,provider=PROVIDER)
        try:
            interpreted=validate_onboarding_result(parsed.data,context)
        except InvalidOutput:
            raise ApiError('AI_FAILED','검증된 일정 후보를 준비하지 못했어요',503) from None
        if not interpreted['isConfirmed'] or interpreted['event'] is None:
            raise ApiError('PARSE_FAILED','날짜 또는 요일과 시작·종료 시각을 명확하게 알려 주세요',400)
        event=EventCreate.model_validate(interpreted['event'])
        validate_voice_evidence(text,event,today=context['today'])
        first=event.date or snapshot.base_date
        if event.days:
            while first.isoweekday() not in event.days: first+=timedelta(days=1)
        if first<snapshot.base_date:
            raise ApiError('PARSE_FAILED','지난 날짜의 일정은 추가할 수 없어요',400)
        names=('MO','TU','WE','TH','FR','SA','SU')
        identifier=str(uuid4())
        try:
            row={'id':identifier,'user_id':user_id,'subject_id':None,'event_type':event.kind,'title':event.title,
                'starts_at':_local_time_to_utc(first,event.start,user['timezone']),
                'ends_at':_local_time_to_utc(first,event.end,user['timezone']),
                'recurrence_rule':'FREQ=WEEKLY;BYDAY='+','.join(names[day-1] for day in event.days) if event.days else None,
                'recurrence_until':None,'source_text':text,'revision':1,'deleted_at':None}
        except ApiError:
            raise ApiError('PARSE_FAILED','시간대에서 확정할 수 있는 날짜와 시각을 알려 주세요',400) from None
        changes={'insertEvents':[_json_value(row)]}
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            if service.capture(user_id,domain=VOICE_DOMAIN,target=target)!=snapshot:
                raise ApiError('PREVIEW_STALE','해석하는 동안 계획이나 일정이 바뀌었어요',409)
            added=_after_occurrences(conn,user_id,snapshot.base_date,{identifier:row},{identifier:[]},{identifier},user['timezone'])
            request,supersede,data,extra=_allocation(conn,user_id,snapshot.base_date,user['timezone'],{identifier:row},{identifier:[]},added)
        if extra.get('placements'): changes['extraAllocation']=extra
        if request is not None:
            try: result=allocate(request)
            except SearchLimit:
                raise ApiError('PLAN_SEARCH_LIMIT','계획 탐색 한도에 도달했어요',503) from None
            if not result.feasible:
                raise ApiError('PLAN_INFEASIBLE','일정 추가 후 남은 공부를 배정할 수 없어요',409)
            changes['allocation']=_saved_allocation(request,result,supersede,data,user['timezone'])
        response={'event':{'title':event.title,'date':first.isoformat(),'start':event.start,'end':event.end,
                'repeat':event.days is not None,'label':'말로 추가한 일정'},
            'moves':(_move_view(data,changes['allocation']) if changes.get('allocation') else [])+_extra_move_view(data,extra),
            'note':None}
        with self.engine.begin() as conn:
            return ProposalService(conn).create(user_id,domain=VOICE_DOMAIN,target=target,snapshot=snapshot,
                normalized_input={'text':text},changes=changes,preview_response=response,
                proposal_type='schedule',description='말로 추가한 일정과 남은 공부 재배치',request_text=text)

    def apply(self,user_id,preview_id):
        with self.engine.begin() as conn:
            return ProposalService(conn).apply(user_id,preview_id,domain=VOICE_DOMAIN,target={'source':'voice_schedule'})
