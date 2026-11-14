"""Immutable previews for moving unfinished work without rewriting study facts."""
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.ranges import PageSpan, subtract, intersect, page_count
from app.domain.scheduling import BusyInterval, Slot, free_slots, half_up, POLICY_VERSION, SearchLimit
from app.repositories.proposals import ProposalRepository, ProposalNotFound, LegacyProposalUnavailable, LegacyResponseUnavailable
from app.services.onboarding_events import _local, _minute
from app.services.schedule_placements import free_calendar_slots
from app.services.plan_draft import _basis, _instant, _fraction, _apply as apply_allocation
from app.services.proposals import PlanEffect, PreviewDomain, ProposalService
from app.services.study import StudyData
from app.services.allocator import allocate
from app.services.plan_inputs import existing_allocation_input, PlanInputError, PendingObligations
from app.services.plan_draft import _present
from app.core.clock import now_utc
from app.services.pace_samples import placement_snapshots, snapshot_for_task, task_factors


def _apply(conn, context, changes):
    if changes.get('allocation') is not None:
        apply_allocation(conn, context, changes['allocation'])
    else:
        for task in changes['moves']:
            source = task['source']
            conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == source['taskId'],
                t.study_tasks.c.plan_id == context.plan.id).values(status='carried_over'))
            conn.execute(t.study_tasks.insert().values(id=task['id'], plan_id=context.plan.id,
                subject_id=task['subjectId'], block_id=source['blockId'], origin_task_id=source['taskId'],
                task_type=task['kind'], title=task['title'], scheduled_date=date.fromisoformat(task['date']),
                starts_at=_instant(task['date'],task['start'],changes['timezone']),
                ends_at=_instant(task['date'],task['end'],changes['timezone']), estimated_minutes=task['minutes'],
                placement_reason='today-'+changes['action'], status='planned', created_at=context.now))
            for order, part in enumerate(task['ranges'],1):
                conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=task['id'],
                    unit_id=part['unitId'],concept_id=part['conceptId'],start_page=part['from'],end_page=part['to'],
                    planned_minutes=part['plannedMinutes'],sort_order=order))
    return {'applied':True}


DOMAIN = PreviewDomain('today_adjustment',PlanEffect.UPDATE,_basis,_apply)


def _remaining(task):
    """Subtract only this placement's actual ranges, preserving PDF/unit identity."""
    rows=[]
    for part in task.ranges:
        if part['start_page'] is None or part['end_page'] is None:
            rows.append({'unitId':part['unit_id'],'materialId':part['material_id'], 'conceptId':part['concept_id'],
                'from':None,'to':None,'plannedMinutes':part['planned_minutes']})
            continue
        completed=[PageSpan(part['unit_id'],row['start_page'],row['end_page']) for row in task.done_ranges
            if row['unit_id']==part['unit_id'] and row['start_page'] is not None and row['end_page'] is not None]
        for span in subtract([PageSpan(part['unit_id'],part['start_page'],part['end_page'])],completed):
            minutes=part['planned_minutes']
            if minutes is not None:
                minutes=half_up(Fraction(minutes*(span.end-span.start+1),part['end_page']-part['start_page']+1))
            rows.append({'unitId':part['unit_id'],'materialId':part['material_id'],'conceptId':part['concept_id'],
                'from':span.start,'to':span.end,'plannedMinutes':minutes})
    return rows


def _movable(task, sessions):
    return (task.row['status'] not in ('completed','carried_over','excluded')
        and not sessions.get(task.id) and (task.record is None or task.record['result'] != 'completed')
        and bool(_remaining(task)))


def _duration(conn,user,data,task):
    if task.row['starts_at'] is not None and task.row['ends_at'] is not None:
        elapsed=task.row['ends_at']-task.row['starts_at']
        original=Fraction(elapsed.days*86400+elapsed.seconds,60)+Fraction(elapsed.microseconds,60_000_000)
    else:
        original=Fraction(task.row['estimated_minutes'])
    if not task.paged or not task.done_ranges:
        return original
    remaining=_remaining(task)
    snapshot=snapshot_for_task(task,{x.id:x for x in data.tasks},placement_snapshots(conn,user['id']),now_utc())
    factors=[]
    if snapshot is not None:
        try:
            factors=[(part,baseline*multiplier) for part,multiplier,baseline in task_factors(task,snapshot)]
        except ValueError:
            pass
    if not factors:
        if all(part['planned_minutes'] is not None for part in task.paged):
            factors=[({'unitId':part['unit_id'],'materialId':part['material_id'],
                'from':part['start_page'],'to':part['end_page']},
                Fraction(part['planned_minutes'],part['end_page']-part['start_page']+1)) for part in task.paged]
        elif len({part['unit_id'] for part in task.paged})==1 and task.pages:
            factors=[({'unitId':part['unit_id'],'materialId':part['material_id'],
                'from':part['start_page'],'to':part['end_page']},original/task.pages) for part in task.paged]
        else:
            raise ApiError('PLAN_INFEASIBLE','복합 태스크의 과거 분량 기준을 확인할 수 없어요',409)
    intervals=defaultdict(list)
    for part,factor in factors:
        bounds=intersect([PageSpan(part['materialId'],part['from'],part['to'])],
            [PageSpan(row['materialId'],row['from'],row['to']) for row in remaining
                if row['unitId']==part['unitId'] and row['from'] is not None])
        for span in bounds: intervals[span.material_id].append((span.start,span.end,factor))
    total=Fraction();covered=0
    for values in intervals.values():
        boundaries=sorted({point for start,end,_ in values for point in (start,end+1)})
        for start,end in zip(boundaries,boundaries[1:]):
            weights={factor for left,right,factor in values if left<=start<=right}
            if len(weights)>1:
                raise ApiError('PLAN_INFEASIBLE','겹치는 범위의 과거 분량 기준이 달라요',409)
            if weights:
                covered+=end-start;total+=(end-start)*next(iter(weights))
    expected=page_count(PageSpan(row['materialId'],row['from'],row['to']) for row in remaining if row['from'] is not None)
    if covered!=expected:
        raise ApiError('PLAN_INFEASIBLE','남은 범위의 과거 분량 기준을 확인할 수 없어요',409)
    return total


def _move(task,slot,minutes):
    return {'id':str(uuid4()), 'subjectId':task.row['subject_id'],'kind':task.row['task_type'],
        'source':{'taskId':task.id,'blockId':task.row['block_id']},'title':task.row['title'],
        'date':slot.day.isoformat(),'start':_fraction(slot.start),'end':_fraction(slot.start+minutes),
        'minutes':half_up(minutes),'ranges':_remaining(task)}


class TodayPlanService:
    def __init__(self,engine): self.engine=engine

    def preview(self,user_id,command):
        target={'action':command.action,'taskId':str(command.taskId) if command.taskId else None}
        with self.engine.begin() as conn:
            if conn.scalar(select(t.plans.c.id).where(t.plans.c.user_id==user_id)) is None:
                raise ApiError('NO_PLAN','아직 승인된 계획이 없어요',400)
            service=ProposalService(conn)
            snapshot=service.capture(user_id,domain=DOMAIN,target=target)
            for candidate in service.repository.ready(user_id,kind=DOMAIN.kind):
                if candidate['target']!=target: continue
                try: service._require_fresh(candidate,snapshot)
                except ApiError: continue
                return candidate['preview_response']
            user=dict(conn.execute(select(t.users).where(t.users.c.id==user_id)).mappings().one())
            data=StudyData(conn,user_id)
            today=snapshot.base_date
            sessions=defaultdict(list)
            for row in conn.execute(select(t.study_sessions).where(t.study_sessions.c.user_id==user_id)).mappings():
                sessions[row['task_id']].append(row)
            if command.action!='rest':
                changes,response=self._single(conn,user,data,sessions,today,command)
                return service.create(user_id,domain=DOMAIN,target=target,snapshot=snapshot,
                    normalized_input=target,changes={**changes,'action':command.action,'timezone':user['timezone']},
                    preview_response=response,proposal_type='today',description='오늘 학습 계획 조정')
            movable=[task for task in data.tasks_on(today) if _movable(task,sessions)]
            if not movable: raise ApiError('NO_TASK','옮길 오늘의 미완료 범위가 없어요',400)
            if any(data.subject_by_id.get(task.row['subject_id'],{}).get('exam_date') is None
                    or data.subject_by_id[task.row['subject_id']]['exam_date']<=today+timedelta(days=1) for task in movable):
                raise ApiError('PLAN_INFEASIBLE','시험일 전에 오늘 범위를 옮길 날짜가 없어요',409)
            try:
                request,supersede,_=existing_allocation_input(conn,user_id,today=today,timezone_name=user['timezone'],
                    subject_ids=(),affected_subject_ids=tuple(sorted({x.row['subject_id'] for x in movable})),
                    earliest_day=today+timedelta(days=1),movable_task_ids=tuple(x.id for x in movable))
            except PendingObligations as error:
                raise ApiError('OVERDUE_OBLIGATIONS',str(error),409) from None
            except PlanInputError as error:
                raise ApiError('INVALID_INPUT',str(error),422) from None
        # Allocation can search many alternatives; no user/DB lock spans it.
        try:
            result=allocate(request)
        except SearchLimit:
            raise ApiError('PLAN_SEARCH_LIMIT','계획 탐색 한도에 도달했어요',503) from None
        if not result.feasible:
            raise ApiError('PLAN_INFEASIBLE','남은 일정에 오늘 범위를 배치할 수 없어요',409)
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            current=service.capture(user_id,domain=DOMAIN,target=target)
            if current!=snapshot:
                raise ApiError('PREVIEW_STALE','계획을 계산하는 동안 입력이 바뀌었어요',409)
            _,placements=_present(conn,user_id,request,result,False)
            obligations={task.id:task for task in data.tasks if task.row['task_type'] in ('review','remedial')}
            for placement in placements:
                keys={part['requirementId'] for part in placement['parts']}
                if placement['kind']!='study':
                    if len(keys)!=1 or not keys<=obligations.keys():
                        raise ApiError('PLAN_INFEASIBLE','보충 의무의 원본을 보존할 수 없어요',409)
                    source=obligations[next(iter(keys))]
                    placement['source']={'taskId':source.id,'blockId':source.row['block_id']}
            allocation={'schemaVersion':'plan-placements-v1','policy':POLICY_VERSION,'timezone':user['timezone'],
                'activateSubjectIds':[],'supersedeIds':supersede,'placements':placements}
            days=defaultdict(int)
            for placement in placements: days[placement['date']]+=placement['minutes']
            response={'subjectName':None,'changes':[{'label':'오늘 미완료 태스크','before':str(len(movable)),'after':'남은 일정에 재배분'}],
                'days':[{'date':day,'text':f'{minutes}분'} for day,minutes in sorted(days.items())],
                'note':'이미 시작한 태스크와 기록한 범위는 유지해요'}
            return service.create(user_id,domain=DOMAIN,target=target,snapshot=snapshot,normalized_input=target,
                changes={'allocation':allocation,'action':'rest','timezone':user['timezone']},
                preview_response=response,proposal_type='today',description='오늘 미완료 범위 재배분')

    def _single(self,conn,user,data,sessions,today,command):
        candidates=[task for task in data.tasks if _movable(task,sessions)
            and task.row['subject_id'] in data.subject_by_id
            and data.subject_by_id[task.row['subject_id']]['exam_date'] is not None]
        if command.action=='more30':
            candidates=[task for task in candidates if task.row['scheduled_date']>today
                and task.record is None and data.subject_by_id[task.row['subject_id']]['exam_date']>today]
            candidates.sort(key=lambda x:(x.row['scheduled_date'],str(x.row['starts_at'] or ''),x.id))
            if not candidates: raise ApiError('NO_TASK','앞당길 미래 태스크가 없어요',400)
            task,day=candidates[0],today
        else:
            task=next((task for task in data.tasks if task.id==str(command.taskId)),None)
            if task is None: raise ApiError('NO_TASK','본인의 태스크를 찾지 못했어요',404)
            if task not in candidates or task.row['scheduled_date']!=today:
                raise ApiError('NO_TASK','오늘의 시작하지 않은 미완료 태스크만 미룰 수 있어요',400)
            future=sorted({x.row['scheduled_date'] for x in data.tasks if x.row['subject_id']==task.row['subject_id']
                and x.row['scheduled_date']>today and x.row['status'] not in ('carried_over','excluded')})
            if not future: raise ApiError('NO_TASK','다음 과목 공부일이 없어요',400)
            day=future[0]
        subject=data.subject_by_id[task.row['subject_id']]
        if day>=subject['exam_date']: raise ApiError('PLAN_INFEASIBLE','시험일 전으로 배치할 수 없어요',409)
        remaining=page_count(PageSpan(r['materialId'],r['from'],r['to']) for r in _remaining(task) if r['from'] is not None)
        minutes=_duration(conn,user,data,task)
        if minutes<15:
            raise ApiError('PLAN_INFEASIBLE','남은 실제 분량이 최소 학습 구간보다 짧아요',409)
        if minutes>60:
            raise ApiError('PLAN_INFEASIBLE','전체 태스크가 한 학습 구간에 들어가지 않아요',409)
        shifted=[]
        if command.action=='postpone':
            shifted=sorted((x for x in candidates if x.id!=task.id and x.row['subject_id']==task.row['subject_id']
                and x.row['scheduled_date']==day and x.record is None),
                key=lambda x:(str(x.row['starts_at'] or ''),x.id))
        ignored=(task.id,*(x.id for x in shifted))
        available=free_calendar_slots(conn,user,[Slot(day,Fraction(8*60),Fraction(23*60))],ignore_task_ids=ignored)
        busy=[]
        cursor=Fraction(8*60)
        for slot in available:
            if cursor<slot.start: busy.append(BusyInterval(day,cursor,slot.start))
            cursor=slot.end
        if cursor<Fraction(23*60): busy.append(BusyInterval(day,cursor,Fraction(23*60)))
        first_fixed=min((_minute(_local(x.row['starts_at'],ZoneInfo(user['timezone']))) for x in data.tasks
            if x.row['subject_id']==task.row['subject_id'] and x.row['scheduled_date']==day and x.id not in ignored
            and x.row['status'] not in ('carried_over','excluded') and x.row['starts_at']),default=Fraction(23*60))
        moves=[]
        lower=Fraction(8*60)
        for current,duration in [(task,minutes),*((x,_duration(conn,user,data,x)) for x in shifted)]:
            slots=free_slots(day,busy)
            slot=next((slot for slot in slots if slot.start>=lower and duration<=slot.minutes
                and (current is not task or command.action!='postpone' or slot.start+duration<=first_fixed)),None)
            if slot is None or not 15<=duration<=60:
                raise ApiError('PLAN_INFEASIBLE','기존 태스크 전체를 옮길 가용 시간이 없어요',409)
            moves.append(_move(current,slot,duration))
            busy.append(BusyInterval(day,slot.start,slot.start+duration))
            lower=slot.start+duration
        return {'moves':moves}, {'subjectName':subject['name'],
            'changes':[{'label':task.row['title'],'before':str(task.row['scheduled_date']),'after':str(day)}],
            'days':[{'date':str(day),'text':f"{moves[0]['minutes']}분 · {remaining}페이지"}], 'note':None}

    def apply(self,user_id,command):
        with self.engine.begin() as conn:
            service=ProposalService(conn)
            try: state=service.repository.get(user_id,str(command.previewId))
            except ProposalNotFound as error: raise ApiError('NO_PREVIEW','미리보기를 찾지 못했어요',404 if error.foreign else 400) from None
            except (LegacyProposalUnavailable,LegacyResponseUnavailable):
                raise ApiError('PREVIEW_STALE','과거 후보의 기준을 확인할 수 없어요',409) from None
            if state['kind']!=DOMAIN.kind or state['target'].get('action')!=command.action:
                raise ApiError('NO_PREVIEW','이 동작의 미리보기가 아니에요',404)
            return service.apply(user_id,str(command.previewId),domain=DOMAIN,target=state['target'])
