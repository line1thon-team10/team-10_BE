"""Shared bounded free-time projection and immutable whole-task moves."""
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.clock import now_utc, today_for
from app.db import schema as t
from app.domain.scheduling import BusyInterval, Slot, free_slots
from app.repositories.events import EventRepository
from app.services.onboarding_events import _local, _minute, event_intervals
from app.services.plan_draft import _instant
from app.services.study import StudyData


def clock_minute(value):
    hour, minute = map(int, value.split(':'))
    return Fraction(hour * 60 + minute)


def clock_text(value):
    minute = int(value)
    return f'{minute // 60:02d}:{minute % 60:02d}'


def free_calendar_slots(conn, user, requested_slots, *, ignore_task_ids=()):
    """Intersect supplied gaps with actual active calendar/task availability."""
    instant = _local(now_utc(), ZoneInfo(user['timezone']))
    today = user.get('demo_date') or instant.date()
    requested = []
    for value in requested_slots:
        slot = value if isinstance(value, Slot) else Slot(date.fromisoformat(value['date']),
            clock_minute(value['start']), clock_minute(value['end']))
        if user.get('demo_date') is None and slot.day == today:
            slot = Slot(slot.day,max(slot.start,_minute(instant)),slot.end)
        if slot.day >= today and slot.start < slot.end:
            requested.append(slot)
    if not requested:
        return ()
    days = sorted({slot.day for slot in requested})
    repo = EventRepository(conn,user['id'])
    events = repo.events()
    exceptions = defaultdict(list)
    for row in repo.exceptions([event['id'] for event in events]):
        exceptions[row['event_id']].append(row)
    busy = [interval for day in days for event in events
        for interval in event_intervals(event,exceptions[event['id']],day,user['timezone']) if interval.day==day]
    data = StudyData(conn,user['id'])
    ignored = set(ignore_task_ids)
    zone = ZoneInfo(user['timezone'])
    for task in data.tasks:
        row = task.row
        if task.id in ignored or row['subject_id'] not in data.subject_by_id or row['status'] in ('carried_over','excluded'):
            continue
        if row['scheduled_date'] not in days:
            continue
        if row['starts_at'] is not None and row['ends_at'] is not None:
            left,right = _local(row['starts_at'],zone),_local(row['ends_at'],zone)
            for day in days:
                if left.date() <= day <= right.date():
                    a = _minute(left) if left.date()==day else Fraction(0)
                    b = _minute(right) if right.date()==day else Fraction(1440)
                    if a < b: busy.append(BusyInterval(day,a,b))
        else:
            # Legacy date-only placements still occupy their promised duration.
            duration=Fraction(row['estimated_minutes'])
            slot=next((slot for slot in free_slots(row['scheduled_date'],busy) if slot.minutes>=duration),None)
            if slot and duration>0:
                busy.append(BusyInterval(slot.day,slot.start,slot.start+duration))
            else:
                # Unknown legacy occupancy cannot be advertised as free time.
                busy.append(BusyInterval(row['scheduled_date'],Fraction(480),Fraction(1380)))
    available=[]
    for day in days:
        intersections=sorted((max(source.start,free.start),min(source.end,free.end))
            for source in requested if source.day==day for free in free_slots(day,busy)
            if max(source.start,free.start)<min(source.end,free.end))
        merged=[]
        for a,b in intersections:
            if merged and a<=merged[-1][1]: merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
            else: merged.append((a,b))
        available.extend(Slot(day,a,b) for a,b in merged)
    return tuple(available)


def move_task(task, slot, timezone_name=None):
    """Save an entire task's range snapshot while retaining its durable book ID."""
    duration=Fraction(task.row['estimated_minutes'])
    if not 15 <= duration <= 60 or slot.minutes < duration:
        raise ValueError('A move needs a continuous 15..60 minute slot')
    return {'id':str(uuid4()),'subjectId':task.row['subject_id'],'blockId':task.row['block_id'],
        'originTaskId':task.id,'kind':task.row['task_type'],'title':task.row['title'],
        'date':slot.day.isoformat(),'start':[slot.start.numerator,slot.start.denominator],
        'end':[(slot.start+duration).numerator,(slot.start+duration).denominator],
        'minutes':int(duration),'ranges':[{key:part[key] for key in
            ('unit_id','concept_id','start_page','end_page','planned_minutes','sort_order')} for part in task.ranges]}


def apply_saved_placements(conn, context, changes):
    """Apply trusted preview snapshots; caller owns user lock and transaction."""
    for identifier in changes.get('supersedeIds',[]):
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id==identifier,
            t.study_tasks.c.plan_id==context.plan.id).values(status='carried_over'))
    for placement in changes.get('placements',[]):
        block = placement.get('blockId') or str(uuid4())
        if placement.get('blockId') is None:
            units={r['unit_id'] for r in placement['ranges']}
            conn.execute(t.study_blocks.insert().values(id=block,subject_id=placement['subjectId'],
                unit_id=next(iter(units)) if len(units)==1 else None,task_type=placement['kind'],created_at=context.now))
        conn.execute(t.study_tasks.insert().values(id=placement['id'],plan_id=context.plan.id,
            subject_id=placement['subjectId'],block_id=block,origin_task_id=placement.get('originTaskId'),
            task_type=placement['kind'],title=placement['title'],scheduled_date=date.fromisoformat(placement['date']),
            starts_at=_instant(placement['date'],placement['start'],context.user.timezone),
            ends_at=_instant(placement['date'],placement['end'],context.user.timezone),estimated_minutes=placement['minutes'],
            placement_reason='schedule-change',status='planned',created_at=context.now))
        for row in placement['ranges']:
            conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()),task_id=placement['id'],**row))
    return {'applied':True}
