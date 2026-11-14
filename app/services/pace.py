"""Persistent P4 proposals with exact evidence and atomic future-only approval."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.clock import now_utc, today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.scheduling import MULTIPLIERS, free_slots, half_up
from app.repositories.events import EventRepository
from app.repositories.locking import lock_user, UserBusyError
from app.repositories.proposals import ProposalRepository, canonical_hash
from app.services.onboarding_events import event_intervals, _local, _minute
from app.services.pace_samples import (aware, collect_samples, encoded, fraction, placement_snapshots,
    task_factors, weighted_pages, snapshot_for_task)
from app.services.plan_draft import _basis
from app.services.proposals import DomainBasis, PlanEffect, PreviewDomain, ProposalService
from app.services.study import StudyData


def _pending_recovery(conn, user_id):
    return conn.scalar(select(t.plan_change_requests.c.id).where(t.plan_change_requests.c.user_id==user_id,
        t.plan_change_requests.c.kind=='recovery',t.plan_change_requests.c.status=='ready')) is not None


def _subject(conn, user_id, subject_id):
    row=conn.execute(select(t.subjects).where(t.subjects.c.id==subject_id,t.subjects.c.user_id==user_id)).mappings().first()
    if row is None:
        raise ApiError('SUBJECT_NOT_FOUND','과목을 찾지 못했어요',404)
    return dict(row)


def _inspect(conn, context, target):
    basis=_basis(conn,context,target)
    subject=_subject(conn,context.user.id,target['subjectId'])
    samples=collect_samples(conn,context.user.to_mapping(),subject,context.today)
    return DomainBasis({**basis.dependency_versions,'paceSamples':canonical_hash({'samples':list(samples.samples)}),
        'pendingRecovery':_pending_recovery(conn,context.user.id)},structure_hash=basis.structure_hash)


def _decimal(value):
    with localcontext() as context:
        context.prec=40
        return (Decimal(value.numerator)/Decimal(value.denominator)).quantize(Decimal('0.0000000001'))


def _apply(conn, context, changes):
    if changes.get('schemaVersion')!='pace-adjustment-v1':
        raise ApiError('PREVIEW_STALE','속도 보정 후보를 다시 확인해 주세요',409)
    conn.execute(update(t.subjects).where(t.subjects.c.id==changes['subjectId'],t.subjects.c.user_id==context.user.id)
        .values(minutes_per_page=_decimal(fraction(changes['baseline'])),updated_at=context.now))
    for placement in changes['placements']:
        old=conn.execute(select(t.study_tasks).where(t.study_tasks.c.id==placement['sourceId'])).mappings().one()
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==old['id']).values(status='carried_over'))
        values={key:old[key] for key in t.study_tasks.c.keys()}
        values.update(id=placement['id'],origin_task_id=old['id'],estimated_minutes=placement['minutes'],
            placement_reason='pace-adjustment-v1',created_at=context.now)
        if old['starts_at'] is not None:
            duration=fraction(placement['duration'])
            values['ends_at']=aware(old['starts_at'])+timedelta(microseconds=half_up(duration*60*1_000_000))
        conn.execute(t.study_tasks.insert().values(**values))
        ranges=conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id==old['id'])).mappings().all()
        for row in ranges:
            values=dict(row);values.update(id=str(uuid4()),task_id=placement['id'])
            part=next((part for part in placement['parts'] if part['unitId']==row['unit_id']
                and part['from']==row['start_page'] and part['to']==row['end_page']),None)
            if part is not None:
                values['planned_minutes']=half_up(fraction(part['baseline'])*fraction(part['multiplier'])*(part['to']-part['from']+1))
            conn.execute(t.study_task_ranges.insert().values(**values))
    conn.execute(update(t.speed_calibrations).where(t.speed_calibrations.c.id==changes['calibrationId']).values(
        status='applied',decided_at=context.now))
    if changes['overDays']:
        from app.services.pace_recovery import request_pace_recovery
        request_pace_recovery(conn,context.user.to_mapping(),context.today,
            [row['id'] for row in changes['placements']],changes['overDays'],at=context.now,
            calibration_id=changes['calibrationId'])
    return {'applied':True,'overDays':[row['date'] for row in changes['overDays']], 'replan':bool(changes['overDays'])}


DOMAIN=PreviewDomain('speed_adjust',PlanEffect.UPDATE,_inspect,_apply)


class PaceService:
    def __init__(self, conn, user, *, clock=now_utc):
        self.conn,self.user,self.clock=conn,user,clock

    def _lock(self):
        try:
            user=lock_user(self.conn,UUID(self.user['id']))
        except UserBusyError:
            raise ApiError('REQUEST_IN_PROGRESS','다른 변경이 진행 중이에요',409) from None
        if user is None: raise ApiError('UNKNOWN_SESSION','사용자를 찾지 못했어요',401)
        self.user=user.to_mapping()
        self.at=self.clock()
        self.today=self.user.get('demo_date') or self.at.astimezone(ZoneInfo(self.user['timezone'])).date()

    def _future(self, data, subject, proposed, snapshots):
        started=set(self.conn.scalars(select(t.study_sessions.c.task_id).where(t.study_sessions.c.user_id==self.user['id'])))
        rows,placements=[],[]
        task_map={task.id:task for task in data.tasks}
        for task in data.tasks:
            row=task.row
            if (row['subject_id']!=subject['id'] or row['scheduled_date']<=self.today
                    or row['status']!='planned' or task.record is not None or task.id in started
                    or (subject['exam_date'] is not None and row['scheduled_date']>=subject['exam_date'])):
                continue
            after,duration,parts=None,None,None
            if task.paged and len(task.paged)==len(task.ranges):
                historical=snapshot_for_task(task,task_map,snapshots,self.at)
                try:
                    factors=task_factors(task,historical) if historical is not None else None
                except (ValueError,KeyError,TypeError,ZeroDivisionError):
                    factors=None
                if factors is None:
                    # Only unstarted future tasks can adopt a current multiplier.
                    parts=[{'unitId':r['unit_id'],'materialId':r['material_id'],'from':r['start_page'],'to':r['end_page'],
                        'baseline':encoded(proposed),'multiplier':encoded(MULTIPLIERS[data.units[r['unit_id']]['difficulty']])}
                        for r in task.paged if r['unit_id'] in data.units]
                    try: factors=task_factors(task,{'parts':parts})
                    except (ValueError,KeyError,TypeError,ZeroDivisionError): factors=None
                if factors is not None:
                    try:
                        _,weighted=weighted_pages(task,factors,completed=False)
                        duration=proposed*weighted
                        after=max(1,half_up(duration))
                        parts=[{**part,'baseline':encoded(proposed)} for part,_,_ in factors]
                    except ValueError: pass
            rows.append({'taskId':task.id,'date':row['scheduled_date'].isoformat(),'title':row['title'],
                'from':task.first,'to':task.last,'ranges':task.range_view.as_dict()['ranges'],'pages':task.pages,
                'now':row['estimated_minutes'],'after':after})
            if after is not None and duration is not None and duration>0:
                placements.append({'id':str(uuid4()),'sourceId':task.id,'subjectId':subject['id'],'date':row['scheduled_date'].isoformat(),
                    'minutes':after,'duration':encoded(duration),'parts':parts})
        return rows,placements

    def _over_days(self,data,placements):
        by_task={p['sourceId']:p for p in placements}
        affected={date.fromisoformat(p['date']) for p in placements}
        repo=EventRepository(self.conn,self.user['id']);events=repo.events();exceptions=defaultdict(list)
        for row in repo.exceptions([e['id'] for e in events]): exceptions[row['event_id']].append(row)
        result=[];zone=ZoneInfo(self.user['timezone'])
        for day in sorted(affected):
            busy=[interval for event in events for interval in event_intervals(event,exceptions[event['id']],day,self.user['timezone']) if interval.day==day]
            free=sum((slot.minutes for slot in free_slots(day,busy)),Fraction(0))
            total=Fraction(0);intervals=[];conflict=False
            for task in data.tasks_on(day):
                placement=by_task.get(task.id)
                duration=fraction(placement['duration']) if placement else Fraction(task.row['estimated_minutes'])
                total+=duration
                if placement and (duration>60 or duration<15): conflict=True
                if task.row['starts_at'] is not None:
                    start=_minute(_local(task.row['starts_at'],zone));end=start+duration
                    if placement and (start<8*60 or end>23*60): conflict=True
                    if placement and any(start<item.end and end>item.start for item in busy): conflict=True
                    intervals.append((start,end,placement is not None))
            for i,(start,end,changed) in enumerate(intervals):
                if any(start<right and end>left and (changed or other) for left,right,other in intervals[:i]): conflict=True
            if total>free or conflict:
                result.append({'date':day.isoformat(),'total':half_up(total),'free':half_up(free),'over':max(0,half_up(total-free)) if total>free else 0})
        return result

    def alert(self):
        self._lock()
        if _pending_recovery(self.conn,self.user['id']): return None
        data=StudyData(self.conn,self.user['id'])
        if data.plan is None: return None
        snapshots=placement_snapshots(self.conn,self.user['id'])
        service=ProposalService(self.conn,clock=self.clock)
        for subject in data.subjects:
            current=self.conn.execute(select(t.speed_calibrations).where(t.speed_calibrations.c.subject_id==subject['id'],
                t.speed_calibrations.c.notice_date==self.today)).mappings().first()
            if current is not None:
                if current['status']!='pending': continue
                candidates=service.repository.ready(self.user['id'],kind=DOMAIN.kind)
                candidate=next((row for row in candidates if row['request_id']==current['change_request_id']),None)
                if candidate is None: continue
                try:
                    snapshot=service.capture(self.user['id'],domain=DOMAIN,target={'subjectId':subject['id']})
                    service._require_fresh(candidate,snapshot)
                except ApiError:
                    self.conn.execute(update(t.speed_calibrations).where(t.speed_calibrations.c.id==current['id']).values(status='stale',decided_at=self.at))
                    continue
                return {k:v for k,v in candidate['preview_response'].items() if k!='previewId'}
            samples=collect_samples(self.conn,self.user,subject,self.today,data=data,snapshots=snapshots)
            if not samples.eligible: continue
            target={'subjectId':subject['id']};snapshot=service.capture(self.user['id'],domain=DOMAIN,target=target)
            identifier=str(uuid4());rows,placements=self._future(data,subject,samples.proposed,snapshots)
            over=self._over_days(data,placements)
            response={'calibrationId':identifier,'subjectId':subject['id'],'subjectName':subject['name'],
                'ratio':float(samples.ratio),'perPage':float(Fraction(samples.measured_seconds,60*samples.completed_pages)),
                'expectedPerPage':float(samples.baseline*samples.weighted_pages/samples.completed_pages),
                'days':samples.days,'sessions':len(samples.samples),'rows':rows,'overDays':over,'mockFilled':[]}
            created=service.create(self.user['id'],domain=DOMAIN,target=target,snapshot=snapshot,
                normalized_input={'calibrationId':identifier},changes={'schemaVersion':'pace-adjustment-v1',
                    'calibrationId':identifier,'subjectId':subject['id'],'baseline':encoded(samples.proposed),
                    'sampleEvidence':list(samples.samples),'placements':placements,'overDays':over},
                preview_response=response,proposal_type='speed_adjust',description='최근 3일 측정에 따른 기준 속도 보정')
            candidate=service.repository.get(self.user['id'],created['previewId'])
            self.conn.execute(t.speed_calibrations.insert().values(id=identifier,subject_id=subject['id'],notice_date=self.today,
                window_start=self.today-timedelta(days=2),window_end=self.today,measured_seconds=samples.measured_seconds,
                completed_pages=samples.completed_pages,previous_minutes_per_page=_decimal(samples.baseline),
                proposed_minutes_per_page=_decimal(samples.proposed),status='pending',change_request_id=candidate['request_id'],created_at=self.at))
            return response
        return None

    def decide(self,subject_id,calibration_id,apply):
        self._lock()
        _subject(self.conn,self.user['id'],subject_id)
        row=self.conn.execute(select(t.speed_calibrations).where(t.speed_calibrations.c.id==calibration_id,
            t.speed_calibrations.c.subject_id==subject_id)).mappings().first()
        if row is None: raise ApiError('NO_PREVIEW','속도 보정 제안을 찾지 못했어요',404)
        proposal=self.conn.scalar(select(t.plan_proposals.c.id).where(t.plan_proposals.c.request_id==row['change_request_id']))
        service=ProposalService(self.conn,clock=self.clock);target={'subjectId':subject_id}
        if apply:
            if row['status'] not in ('pending','applied'): raise ApiError('PREVIEW_STALE','이미 끝난 보정 제안이에요',409)
            return service.apply(self.user['id'],proposal,domain=DOMAIN,target=target)
        if row['status']=='dismissed': return {'applied':False,'overDays':[],'replan':False}
        if row['status']!='pending': raise ApiError('PREVIEW_STALE','이미 끝난 보정 제안이에요',409)
        service.cancel(self.user['id'],proposal,domain=DOMAIN,target=target)
        self.conn.execute(update(t.speed_calibrations).where(t.speed_calibrations.c.id==calibration_id).values(status='dismissed',decided_at=self.at))
        return {'applied':False,'overDays':[],'replan':False}
