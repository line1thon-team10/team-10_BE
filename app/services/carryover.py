"""Append-only carry placements; the caller owns the user lock and transaction."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo
from math import ceil
from uuid import uuid4

from sqlalchemy import select, update

from app.db import schema as t
from app.domain.ranges import PageSpan, page_count
from app.domain.scheduling import BusyInterval, free_slots
from app.repositories.events import EventRepository
from app.services.onboarding_events import event_intervals


def _slot(conn, source, day, minutes):
    user = conn.execute(select(t.users).join(t.subjects, t.subjects.c.user_id == t.users.c.id)
                        .where(t.subjects.c.id == source.row['subject_id'])).mappings().one()
    repository = EventRepository(conn, user['id'])
    events = repository.events()
    exceptions = defaultdict(list)
    for item in repository.exceptions([event['id'] for event in events]):
        exceptions[item['event_id']].append(item)
    busy = [part for event in events for part in event_intervals(event, exceptions[event['id']], day, user['timezone']) if part.day == day]
    rows = list(conn.execute(select(t.study_tasks).where(t.study_tasks.c.plan_id == source.row['plan_id'],
        t.study_tasks.c.scheduled_date == day, t.study_tasks.c.status.not_in(['excluded', 'carried_over']),
        t.study_tasks.c.id != source.id)).mappings())
    tz = ZoneInfo(user['timezone'])
    for row in sorted(rows, key=lambda r: (r['starts_at'] is None, r['created_at'], r['id'])):
        if row['starts_at'] is not None and row['ends_at'] is not None:
            local = lambda value: (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(tz)
            start, end = local(row['starts_at']), local(row['ends_at'])
            minute = lambda value: Fraction(value.hour * 60 + value.minute) + Fraction(value.second, 60)
            busy.append(BusyInterval(day, minute(start), minute(end)))
        else:
            slot = next((slot for slot in free_slots(day, busy) if slot.minutes >= row['estimated_minutes']), None)
            if slot is None:
                return None
            busy.append(BusyInterval(day, slot.start, slot.start + row['estimated_minutes']))
    slot = next((slot for slot in free_slots(day, busy) if slot.minutes >= minutes), None)
    if slot is None:
        return None
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz) + timedelta(seconds=float(slot.start * 60))
    end = start + timedelta(minutes=minutes)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def append_carry(conn, source, ranges, *, day, reason, at, minutes=None):
    """Keep source facts and same block; return the concrete persisted placement."""
    if not ranges:
        return None
    existing = conn.execute(select(t.study_tasks).where(t.study_tasks.c.origin_task_id == source.id,
        t.study_tasks.c.status.in_(['planned', 'partial', 'completed']))).mappings().first()
    if existing is not None:
        # Repeated day assessment must not append a sibling obligation.
        return None
    material = lambda r: r.get('material_id', r['unit_id'])
    pages = page_count(PageSpan(material(r), r['start_page'], r['end_page']) for r in ranges)
    source_pages = page_count(PageSpan(material(r), r['start_page'], r['end_page']) for r in source.paged)
    if minutes is None:
        # Retain this placement's pace rather than replacing it with today's b.
        minutes = max(1, ceil(Fraction(source.row['estimated_minutes'] * pages, max(1, source_pages))))
    if not 15 <= minutes <= 60:
        return None
    slot = _slot(conn, source, day, minutes)
    if slot is None:
        return None
    task_id = str(uuid4())
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == source.id).values(status='carried_over'))
    conn.execute(t.study_tasks.insert().values(id=task_id, plan_id=source.row['plan_id'],
        subject_id=source.row['subject_id'], block_id=source.row['block_id'], origin_task_id=source.id,
        task_type=source.row['task_type'], title=source.row['title'], scheduled_date=day,
        starts_at=slot[0], ends_at=slot[1], estimated_minutes=minutes, placement_reason=reason, status='planned', created_at=at))
    persisted = []
    for index, item in enumerate(ranges, 1):
        rid = str(uuid4())
        conn.execute(t.study_task_ranges.insert().values(id=rid, task_id=task_id, unit_id=item['unit_id'],
            start_page=item['start_page'], end_page=item['end_page'], sort_order=index))
        persisted.append({'id': rid, 'rangeId': rid, 'sourceRangeId': item.get('source_range_id'),
            'materialId': material(item), 'unitId': item['unit_id'],
            'from': item['start_page'], 'to': item['end_page']})
    # Fragmented or multi-PDF ranges must never be presented as one continuous span.
    return {'taskId': task_id, 'originTaskId': source.id, 'blockId': source.row['block_id'],
        'subjectId': source.row['subject_id'], 'ranges': persisted, 'pages': pages,
        'from': persisted[0]['from'] if len(persisted) == 1 else None,
        'to': persisted[0]['to'] if len(persisted) == 1 else None,
        'date': day.isoformat(), 'minutes': minutes}
