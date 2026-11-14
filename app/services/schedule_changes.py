"""Versioned calendar occurrence changes preserving original history."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.onboarding import event_view
from app.repositories.events import EventRepository
from app.repositories.proposals import ProposalRepository, ProposalNotFound, LegacyProposalUnavailable, LegacyResponseUnavailable
from app.services.calendar import occurrences
from app.services.empty_time import EmptyTimeService
from app.services.onboarding_events import _local, _local_time_to_utc
from app.services.plan_draft import _basis, _json_value
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService


def occurrence_key(value):
    try:
        identifier,day=value.split('@')
        parsed=date.fromisoformat(day)
        if parsed.isoformat()!=day: raise ValueError
        return str(UUID(identifier)),parsed
    except (ValueError,TypeError,AttributeError):
        raise ApiError('SCHEDULE_NOT_FOUND','일정 회차를 찾지 못했어요',404) from None


def canonical_occurrence(value):
    identifier,day=occurrence_key(value)
    return f'{identifier}@{day.isoformat()}'


def _decode_event(values):
    return {key:datetime.fromisoformat(value) if value is not None and key in ('starts_at','ends_at','deleted_at','created_at','updated_at')
        else date.fromisoformat(value) if value is not None and key=='recurrence_until' else value for key,value in values.items()}


def _decode_exception(values):
    return {key:datetime.fromisoformat(value) if value is not None and key in ('new_starts_at','new_ends_at','created_at','updated_at')
        else date.fromisoformat(value) if key=='occurrence_date' else value for key,value in values.items()}


def _persist(conn,context,changes):
    for event in changes.get('updateEvents',[]):
        values=_decode_event(event['values'])
        conn.execute(update(t.calendar_events).where(t.calendar_events.c.id==event['id'],
            t.calendar_events.c.user_id==context.user.id).values(**values,revision=t.calendar_events.c.revision+1,updated_at=context.now))
    for exception in changes.get('exceptions',[]):
        values=_decode_exception(exception['values'])
        if exception.get('insert'):
            conn.execute(t.calendar_event_exceptions.insert().values(id=exception['id'],**values,created_at=context.now,updated_at=context.now))
        else:
            conn.execute(update(t.calendar_event_exceptions).where(t.calendar_event_exceptions.c.id==exception['id'])
                .values(**values,updated_at=context.now))
    return {'applied':True,'releasedSlots':changes.get('releasedSlots',[])}


def _deleted(conn,context,response):
    slots=response['releasedSlots']
    if not slots:
        return {'emptyTime':None,'slots':[],'options':[],'previewId':None}
    return EmptyTimeService(conn,context.user.to_mapping()).offer(slots)


DELETE_DOMAIN=PreviewDomain('schedule_delete',PlanEffect.UPDATE,_basis,_persist,after_apply=_deleted)


def _load(conn,user_id,key,today,expected_revision=None):
    identifier,original=occurrence_key(key)
    repo=EventRepository(conn,user_id)
    event=repo.event(identifier)
    if event is None:
        raise ApiError('SCHEDULE_NOT_FOUND','일정 회차를 찾지 못했어요',404)
    view=event_view(event,conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id)))
    start=_local(event['starts_at'],ZoneInfo(conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id)))).date()
    occurs=(original==start if view['days'] is None else original>=start and original.isoweekday() in view['days']
        and (event['recurrence_until'] is None or original<=event['recurrence_until']))
    exceptions=repo.exceptions([identifier])
    current=next((x for x in exceptions if x['occurrence_date']==original),None)
    if not occurs or current and current['kind']=='cancelled':
        raise ApiError('SCHEDULE_NOT_FOUND','삭제되었거나 존재하지 않는 일정 회차예요',404)
    if original<today:
        raise ApiError('INVALID_INPUT','지난 회차의 일정은 변경할 수 없어요',422)
    if expected_revision is not None and event['revision']!=expected_revision:
        raise ApiError('PREVIEW_STALE','일정이 바뀌었어요. 다시 확인해 주세요',409)
    return event,exceptions,original


def _horizon(conn,user_id,today,original):
    exams=list(conn.scalars(select(t.subjects.c.exam_date).where(t.subjects.c.user_id==user_id,
        t.subjects.c.status=='active',t.subjects.c.exam_date.is_not(None))))
    end=max([today,original,*exams])
    if (end-today).days>366:
        raise ApiError('PLAN_SEARCH_LIMIT','일정 계산 기간이 너무 길어요. 시험 날짜를 확인해 주세요',503)
    return end


def _slots(event,exceptions,first,last,zone,*,original=None,from_original=None):
    return [{'date':row['date'],'start':row['start'],'end':row['end'],'label':row['title']}
        for row in occurrences(event,exceptions,first,last,zone)
        if (original is None or row['id'].endswith('@'+original.isoformat()))
        and (from_original is None or date.fromisoformat(row['id'].split('@')[1])>=from_original)]


class ScheduleChangesService:
    def __init__(self,engine):
        self.engine=engine

    def delete_preview(self,user_id,key,scope,revision):
        key=canonical_occurrence(key)
        target={'occurrenceId':key}
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            snapshot=service.capture(user_id,domain=DELETE_DOMAIN,target=target)
            event,exceptions,original=_load(conn,user_id,key,snapshot.base_date,revision)
            zone=conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id))
            if scope=='future' and not event['recurrence_rule']:
                raise ApiError('INVALID_INPUT','한 번뿐인 일정은 이번 회차만 삭제할 수 있어요',422)
            horizon=_horizon(conn,user_id,snapshot.base_date,original)
            released=_slots(event,exceptions,snapshot.base_date,horizon,zone,
                original=original if scope=='once' else None,from_original=original if scope=='future' else None)
            changes={'updateEvents':[],'exceptions':[],'releasedSlots':released}
            if scope=='future':
                changes['updateEvents'].append({'id':event['id'],'values':{'recurrence_until':(original-timedelta(days=1)).isoformat()}})
            else:
                previous=next((x for x in exceptions if x['occurrence_date']==original),None)
                changes['updateEvents'].append({'id':event['id'],'values':{}})
                changes['exceptions'].append({'id':previous['id'] if previous else str(uuid4()),'insert':previous is None,
                    'values':{'event_id':event['id'],'occurrence_date':original.isoformat(),'kind':'cancelled',
                        'new_starts_at':None,'new_ends_at':None}})
            response={'changes':[{'label':'일정','before':event['title'],'after':'이번 회차 삭제' if scope=='once' else '이후 반복 종료'}],
                'days':[{'date':row['date'],'text':row['start']+'–'+row['end']+' 비워짐'} for row in released],
                'note':'기존 학습 기록과 태스크는 유지됩니다.'}
            return service.create(user_id,domain=DELETE_DOMAIN,target=target,snapshot=snapshot,
                normalized_input={'scope':scope,'expectedRevision':revision},changes=changes,preview_response=response,
                proposal_type='schedule',description='일정 회차 삭제')

    def apply(self,user_id,key,preview_id):
        key=canonical_occurrence(key)
        with self.engine.begin() as conn:
            return ProposalService(conn).apply(user_id,preview_id,domain=DELETE_DOMAIN,
                target={'occurrenceId':key})
