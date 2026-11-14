"""Versioned calendar occurrence changes preserving original history."""
from collections import defaultdict
from dataclasses import replace
from fractions import Fraction
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.onboarding import event_view
from app.domain.ranges import PageSpan, intersect, page_count, subtract
from app.domain.scheduling import POLICY_VERSION, SearchLimit, Slot, free_slots, half_up
from app.repositories.events import EventRepository
from app.repositories.proposals import ProposalRepository, ProposalNotFound, LegacyProposalUnavailable, LegacyResponseUnavailable
from app.services.calendar import occurrences
from app.services.empty_time import EmptyTimeService
from app.services.onboarding_events import _local, _local_time_to_utc
from app.services.plan_draft import _basis, _json_value
from app.services.plan_inputs import existing_allocation_input, PlanInputError, PendingObligations
from app.services.allocator import allocate
from app.services.study import StudyData
from app.services.schedule_placements import apply_saved_placements, clock_minute, clock_text, move_task
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
    for event in changes.get('insertEvents',[]):
        conn.execute(t.calendar_events.insert().values(**_decode_event(event),created_at=context.now,updated_at=context.now))
    for exception in changes.get('exceptions',[]):
        values=_decode_exception(exception['values'])
        if exception.get('insert'):
            conn.execute(t.calendar_event_exceptions.insert().values(id=exception['id'],**values,created_at=context.now,updated_at=context.now))
        else:
            conn.execute(update(t.calendar_event_exceptions).where(t.calendar_event_exceptions.c.id==exception['id'])
                .values(**values,updated_at=context.now))
    if changes.get('allocation'):
        from app.services.plan_draft import _apply
        _apply(conn,context,changes['allocation'])
    if changes.get('extraAllocation'):
        apply_saved_placements(conn,context,changes['extraAllocation'])
    return {'applied':True,'releasedSlots':changes.get('releasedSlots',[])}


def _deleted(conn,context,response):
    slots=response['releasedSlots']
    if not slots:
        return {'emptyTime':None,'slots':[],'options':[],'previewId':None}
    return EmptyTimeService(conn,context.user.to_mapping()).offer(slots)


def _edited(conn,context,response):
    return {'applied':True}


EDIT_DOMAIN=PreviewDomain('schedule_edit',PlanEffect.UPDATE,_basis,_persist,after_apply=_edited)


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

    def edit_preview(self,user_id,key,command):
        return _edit_preview(self,user_id,key,command)

    def apply(self,user_id,key,preview_id,*,edit=False):
        key=canonical_occurrence(key)
        with self.engine.begin() as conn:
            return ProposalService(conn).apply(user_id,preview_id,domain=EDIT_DOMAIN if edit else DELETE_DOMAIN,
                target={'occurrenceId':key})


def _recurs(event,day,zone):
    first=_local(event['starts_at'],ZoneInfo(zone)).date()
    weekdays=event_view(event,zone)['days']
    return day==first if weekdays is None else day>=first and day.isoweekday() in weekdays and (
        event['recurrence_until'] is None or day<=event['recurrence_until'])


def _edit_candidate(event,exceptions,original,command,zone):
    changes={'updateEvents':[],'insertEvents':[],'exceptions':[],'exceptionChanges':[]}
    overrides,exception_overrides={},{}
    if command.scope=='once':
        left=_local_time_to_utc(command.event.date,command.event.start,zone)
        right=_local_time_to_utc(command.event.date,command.event.end,zone)
        previous=next((x for x in exceptions if x['occurrence_date']==original),None)
        moved={'id':previous['id'] if previous else str(uuid4()),'event_id':event['id'],'occurrence_date':original,
            'kind':'moved','new_starts_at':left,'new_ends_at':right}
        changes['updateEvents'].append({'id':event['id'],'values':{}})
        changes['exceptions'].append({'id':moved['id'],'insert':previous is None,
            'values':_json_value({key:value for key,value in moved.items() if key!='id'})})
        exception_overrides[event['id']]=[row for row in exceptions if row['occurrence_date']!=original]+[moved]
        overrides[event['id']]=event
        added_ids={f"{event['id']}@{original.isoformat()}"}
        shown={'title':event['title'],'date':command.event.date.isoformat(),'start':command.event.start,
            'end':command.event.end,'repeat':False,'label':'이번 회차 이동'}
    else:
        if not event['recurrence_rule'] or command.event.startDate<original:
            raise ApiError('INVALID_INPUT','반복 일정의 기준일 이후부터 새 반복을 시작해 주세요',422)
        names=('MO','TU','WE','TH','FR','SA','SU')
        identifier=str(uuid4())
        new={**event,'id':identifier,'event_type':command.event.kind,'title':command.event.title,
            'starts_at':_local_time_to_utc(command.event.startDate,command.event.start,zone),
            'ends_at':_local_time_to_utc(command.event.startDate,command.event.end,zone),
            'recurrence_rule':'FREQ=WEEKLY;BYDAY='+','.join(names[day-1] for day in command.event.days),
            'recurrence_until':command.event.until,'revision':1,'deleted_at':None}
        old={**event,'recurrence_until':original-timedelta(days=1)}
        overrides={event['id']:old,identifier:new}
        changes['updateEvents'].append({'id':event['id'],'values':{'recurrence_until':old['recurrence_until'].isoformat()}})
        changes['insertEvents'].append(_json_value({key:value for key,value in new.items() if key not in ('created_at','updated_at')}))
        kept,transferred=[],[]
        for row in exceptions:
            action='keep'
            if row['occurrence_date']>=original:
                if _recurs(new,row['occurrence_date'],zone):
                    action='transfer'
                    transferred.append({**row,'event_id':identifier})
                    changes['exceptions'].append({'id':row['id'],'insert':False,'values':{'event_id':identifier}})
                else:
                    action='discard'
                    kept.append(row)
            else:
                kept.append(row)
            changes['exceptionChanges'].append({'id':row['id'],'occurrenceDate':row['occurrence_date'].isoformat(),
                'action':action,'reason':'새 반복에도 같은 원래 회차가 있어요' if action=='transfer' else
                '과거 회차를 유지해요' if action=='keep' else '새 반복에서 사라지는 회차예요. 과거 이력은 보존해요'})
        exception_overrides={event['id']:kept,identifier:transferred}
        added_ids={identifier}
        shown={'title':new['title'],'date':command.event.startDate.isoformat(),'start':command.event.start,
            'end':command.event.end,'repeat':True,'label':'이후 반복 변경'}
    return changes,overrides,exception_overrides,added_ids,shown


def _after_occurrences(conn,user_id,first,overrides,exception_overrides,added_ids,zone):
    repo=EventRepository(conn,user_id)
    events={row['id']:row for row in repo.events()}
    exceptions=defaultdict(list)
    for row in repo.exceptions(list(events)): exceptions[row['event_id']].append(row)
    for identifier,row in overrides.items():
        if row is None: events.pop(identifier,None)
        else: events[identifier]=row
    exceptions.update(exception_overrides)
    # Two complete weekly periods after the last first-occurrence/exception
    # cover recurrence intersections without iterating unbounded UNTIL rules.
    starts=[_local(row['starts_at'],ZoneInfo(zone)).date() for row in events.values()]
    moved=[_local(row['new_starts_at'],ZoneInfo(zone)).date() for rows in exceptions.values() for row in rows if row.get('new_starts_at')]
    exams=list(conn.scalars(select(t.subjects.c.exam_date).where(t.subjects.c.user_id==user_id,
        t.subjects.c.status=='active',t.subjects.c.exam_date.is_not(None))))
    anchor=max([first,*starts,*moved,*exams])
    if (anchor-first).days>352 or anchor>date.max-timedelta(days=14):
        raise ApiError('PLAN_SEARCH_LIMIT','일정 충돌을 계산할 기간이 너무 길어요',503)
    last=anchor+timedelta(days=14)
    if (last-first).days>366:
        raise ApiError('PLAN_SEARCH_LIMIT','일정 충돌을 계산할 기간이 너무 길어요',503)
    all_rows=[item for event in events.values() for item in occurrences(event,exceptions[event['id']],first,last,zone)]
    added=[row for row in all_rows if row['id'] in added_ids or row['scheduleId'] in added_ids]
    others=[row for row in all_rows if row not in added]
    for index,row in enumerate(added):
        for other in [*others,*added[index+1:]]:
            if row['date']==other['date'] and row['start']<other['end'] and other['start']<row['end']:
                raise ApiError('SCHEDULE_CONFLICT','다른 고정 일정과 시간이 겹쳐요',409)
    return added


def _task_collisions(data,added,zone):
    result=[]
    for task in data.tasks:
        row=task.row
        if row['status'] in ('carried_over','excluded') or row['subject_id'] not in data.subject_by_id: continue
        for event in added:
            if row['scheduled_date'].isoformat()!=event['date']: continue
            if row['starts_at'] is None or row['ends_at'] is None:
                result.append(task.id);break
            left,right=_local(row['starts_at'],ZoneInfo(zone)),_local(row['ends_at'],ZoneInfo(zone))
            if left.strftime('%H:%M')<event['end'] and event['start']<right.strftime('%H:%M'):
                result.append(task.id);break
    return tuple(result)


def _allocation(conn,user_id,today,zone,overrides,exception_overrides,added):
    data=StudyData(conn,user_id)
    collisions=_task_collisions(data,added,zone)
    if not collisions: return None,[],data,{}
    try:
        request,supersede,_=existing_allocation_input(conn,user_id,today=today,timezone_name=zone,subject_ids=(),
            calendar_event_overrides=overrides,calendar_exception_overrides=exception_overrides,movable_task_ids=collisions)
    except PendingObligations as error:
        raise ApiError('OVERDUE_OBLIGATIONS',str(error),409) from None
    except PlanInputError as error:
        raise ApiError('INVALID_INPUT',str(error),422) from None
    # A schedule edit moves already planned work; it does not silently add
    # previously unscheduled study pages to the approved plan.
    planned=defaultdict(list)
    for task in data.tasks:
        if task.row['status'] in ('carried_over','excluded') or task.row['task_type']!='study': continue
        for part in task.paged:
            planned[part['unit_id']].append(PageSpan(part['material_id'],part['start_page'],part['end_page']))
    request=replace(request,units=tuple(replace(unit,ranges=intersect(unit.ranges,planned[unit.id])) for unit in request.units))
    # Time-only reviews have no page requirement. Move the original duration as
    # a whole obligation, without manufacturing page ranges or completion.
    consumed=set(conn.scalars(select(t.study_records.c.session_id).where(t.study_records.c.session_id.is_not(None))))
    protected=set(conn.scalars(select(t.study_sessions.c.task_id).where(t.study_sessions.c.user_id==user_id,
        (t.study_sessions.c.status.in_(('running','paused'))) | (~t.study_sessions.c.id.in_(consumed)))))
    movable={task.id:task for task in data.tasks if task.id in collisions and not task.paged
        and task.row['status'] in ('planned','partial','missed') and task.id not in protected
        and (task.record is None or task.record['result']=='none')}
    extra={'placements':[],'supersedeIds':[]}
    fixed=[task for task in request.fixed if task.id not in movable]
    fixed_by_id={task.id:task for task in request.fixed}
    for identifier,task in sorted(movable.items()):
        duration=Fraction(task.row['estimated_minutes'])
        if not 15<=duration<=60:
            raise ApiError('PLAN_INFEASIBLE','복습을 보존할 15~60분의 연속 시간이 필요해요',409)
        exam=data.subject_by_id[task.row['subject_id']]['exam_date']
        candidate=None
        day=max(today,task.row['scheduled_date'])
        while exam and day<exam:
            candidate=next((slot for slot in free_slots(day,request.busy,fixed) if slot.minutes>=duration),None)
            if candidate is not None: break
            day+=timedelta(days=1)
        if candidate is None:
            raise ApiError('PLAN_INFEASIBLE','시험 전 복습 시간을 보존할 수 없어요',409)
        extra['placements'].append(move_task(task,candidate))
        extra['supersedeIds'].append(identifier)
        fixed.append(replace(fixed_by_id[identifier],day=candidate.day,start=candidate.start,end=candidate.start+duration))
    return replace(request,fixed=tuple(fixed)),supersede,data,extra


def _saved_allocation(request,result,supersede,data,zone):
    def pair(value): return [value.numerator,value.denominator]
    units=data.units
    placements=[]
    for task in result.placements:
        parts=[{'requirementId':part.requirement_id,'unitId':part.unit_id,'materialId':part.span.material_id,
            'from':part.span.start,'to':part.span.end,'baseline':pair(part.baseline_minutes_per_page),
            'multiplier':pair(part.difficulty_multiplier)} for part in task.parts]
        placements.append({'id':str(uuid4()),'subjectId':task.subject_id,'kind':task.kind,'date':task.day.isoformat(),
            'start':pair(task.start),'end':pair(task.end),'minutes':task.display_minutes,
            'title':' · '.join(dict.fromkeys(units[part.unit_id]['title'] if part.unit_id in units else next(
                (old.row['title'] for old in data.tasks if old.id==part.requirement_id),'복습') for part in task.parts)), 'parts':parts})
    source_tasks={task.id:task for task in data.tasks if task.id in supersede}
    for placement in placements:
        requirements={part['requirementId'] for part in placement['parts']}
        if placement['kind']!='study':
            if len(requirements)!=1 or not requirements<=source_tasks.keys():
                raise ApiError('PLAN_INFEASIBLE','기존 복습 의무의 식별자를 보존할 수 없어요',409)
            source=source_tasks[next(iter(requirements))]
            if sum(any(part['requirementId']==source.id for part in candidate['parts']) for candidate in placements)!=1:
                raise ApiError('PLAN_INFEASIBLE','기존 복습은 하나의 연속 시간에 배정해야 해요',409)
            placement['source']={'taskId':source.id,'blockId':source.row['block_id']}
            continue
        actual=tuple(PageSpan(part['unitId'],part['from'],part['to']) for part in placement['parts'])
        matches=[]
        for source in source_tasks.values():
            if source.row['task_type']!='study' or source.row['subject_id']!=placement['subjectId']: continue
            planned=[PageSpan(part['unit_id'],part['start_page'],part['end_page']) for part in source.paged]
            done=[PageSpan(part['unit_id'],part['start_page'],part['end_page']) for part in source.done_ranges
                if part['start_page'] is not None and part['end_page'] is not None]
            remaining=subtract(planned,done)
            if remaining and not subtract(remaining,actual) and not subtract(actual,remaining): matches.append(source)
        if len(matches)==1:
            source=matches[0]
            placement['source']={'taskId':source.id,'blockId':source.row['block_id']}
    return {'schemaVersion':'plan-placements-v1','policy':POLICY_VERSION,'timezone':zone,
        'activateSubjectIds':[],'supersedeIds':supersede,'placements':placements}


def _move_view(data,changes):
    moves=[]
    for task in data.tasks:
        if task.id not in changes['supersedeIds']: continue
        pieces=[]
        for placement in changes['placements']:
            cursor=Fraction(*placement['start'])
            for part in placement['parts']:
                cost=Fraction(*part['baseline'])*Fraction(*part['multiplier'])
                for old in task.paged:
                    if old['unit_id']!=part['unitId']: continue
                    matches=intersect([PageSpan(part['materialId'],part['from'],part['to'])],
                        [PageSpan(old['material_id'],old['start_page'],old['end_page'])])
                    for match in matches:
                        start=cursor+(match.start-part['from'])*cost
                        end=start+(match.end-match.start+1)*cost
                        ranges=[{'id':placement['id']+':'+str(match.start),'materialId':match.material_id,'unitId':part['unitId'],
                            'conceptId':None,'from':match.start,'to':match.end,'doneTo':None}]
                        pieces.append({'date':placement['date'],'start':clock_text(start),'end':clock_text(end),
                            'minutes':half_up(end-start),'from':match.start,'to':match.end,'pages':match.end-match.start+1,'ranges':ranges})
                cursor+=(part['to']-part['from']+1)*cost
        subject=data.subject_by_id[task.row['subject_id']]
        block={**data.task_view(task),'subjectName':subject['name'],'color':subject['color'],
            'date':task.row['scheduled_date'].isoformat(),
            'start':_local(task.row['starts_at'],ZoneInfo(changes['timezone'])).strftime('%H:%M') if task.row['starts_at'] else '',
            'end':_local(task.row['ends_at'],ZoneInfo(changes['timezone'])).strftime('%H:%M') if task.row['ends_at'] else '',
            'moved':task.row['origin_task_id'] is not None}
        moves.append({'taskId':task.id,'block':block,'pieces':pieces})
    return moves


def _edit_preview(self,user_id,key,command):
    key=canonical_occurrence(key);target={'occurrenceId':key}
    with self.engine.begin() as conn:
        service=ProposalService(conn);snapshot=service.capture(user_id,domain=EDIT_DOMAIN,target=target)
        event,exceptions,original=_load(conn,user_id,key,snapshot.base_date,command.expectedRevision)
        zone=conn.scalar(select(t.users.c.timezone).where(t.users.c.id==user_id))
        if command.scope=='once' and command.event.date<snapshot.base_date:
            raise ApiError('INVALID_INPUT','지난 날짜로 일정을 이동할 수 없어요',422)
        changes,overrides,exception_overrides,added_ids,shown=_edit_candidate(event,exceptions,original,command,zone)
        added=_after_occurrences(conn,user_id,snapshot.base_date,overrides,exception_overrides,added_ids,zone)
        request,supersede,data,extra=_allocation(conn,user_id,snapshot.base_date,zone,overrides,exception_overrides,added)
    if extra.get('placements'): changes['extraAllocation']=extra
    if request is not None:
        try: result=allocate(request)
        except SearchLimit:
            raise ApiError('PLAN_SEARCH_LIMIT','계획 탐색 한도에 도달했어요',503) from None
        if not result.feasible:
            raise ApiError('PLAN_INFEASIBLE','일정 변경 후 남은 공부를 배정할 수 없어요',409)
        changes['allocation']=_saved_allocation(request,result,supersede,data,zone)
    with self.engine.begin() as conn:
        service=ProposalService(conn)
        response={'event':shown,'moves':(_move_view(data,changes['allocation']) if changes.get('allocation') else []) + _extra_move_view(data,extra),
            'exceptionChanges':changes['exceptionChanges'],'note':None}
        return service.create(user_id,domain=EDIT_DOMAIN,target=target,snapshot=snapshot,
            normalized_input=command.model_dump(mode='json'),changes=changes,preview_response=response,
            proposal_type='schedule',description='일정 시간 변경과 남은 공부 재배치')



def _extra_move_view(data,extra):
    result=[]
    by_id={task.id:task for task in data.tasks}
    for placement in extra.get('placements',[]):
        task=by_id[placement['originTaskId']]
        subject=data.subject_by_id[task.row['subject_id']]
        piece={'date':placement['date'],'start':clock_text(Fraction(*placement['start'])),
            'end':clock_text(Fraction(*placement['end'])),'minutes':placement['minutes'],
            'from':None,'to':None,'pages':0,'ranges':[]}
        result.append({'taskId':task.id,'block':{**data.task_view(task),'subjectName':subject['name'],'color':subject['color'],
            'date':task.row['scheduled_date'].isoformat(),'start':'','end':'','moved':False},'pieces':[piece]})
    return result
