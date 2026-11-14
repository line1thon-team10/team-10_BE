"""Owned calendar projection retaining the original occurrence identity."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import today_for
from app.domain.onboarding import event_view
from app.repositories.events import EventRepository
from app.services.onboarding_events import _local
from app.services.study import StudyData


def occurrences(row, exceptions, first, last, timezone_name):
    tz = ZoneInfo(timezone_name)
    start, end = _local(row['starts_at'], tz), _local(row['ends_at'], tz)
    weekdays = event_view(row, timezone_name)['days']
    duration = (end.date() - start.date()).days
    changes = {x['occurrence_date']: x for x in exceptions}
    def occurs(day):
        if weekdays is None:
            return day == start.date()
        return (day >= start.date() and day.isoweekday() in weekdays
                and (row['recurrence_until'] is None or day <= row['recurrence_until']))
    earliest = max(start.date(), first - timedelta(days=min(duration, (first-date.min).days)))
    dates = {earliest + timedelta(days=i) for i in range(max(0, (last-earliest).days + 1))}
    dates.update(changes)
    if weekdays is None:
        dates.add(start.date())
    for original in sorted(dates):
        if not occurs(original):
            continue
        change = changes.get(original)
        if change and change['kind'] == 'cancelled':
            continue
        if change:
            left, right = _local(change['new_starts_at'], tz), _local(change['new_ends_at'], tz)
        else:
            left = datetime.combine(original, start.timetz())
            right = datetime.combine(original + timedelta(days=duration), end.timetz())
        for offset in range((last-first).days + 1):
            day = first + timedelta(days=offset)
            if left.date() <= day <= right.date():
                a = left.strftime('%H:%M') if day == left.date() else '00:00'
                b = right.strftime('%H:%M') if day == right.date() else '24:00'
                if a < b:
                    yield {'id': f"{row['id']}@{original.isoformat()}", 'scheduleId': row['id'],
                        'revision': row['revision'], 'kind': row['event_type'], 'title': row['title'],
                        'date': day.isoformat(), 'start': a, 'end': b, 'repeat': weekdays is not None, 'added': False}


class CalendarService:
    def __init__(self, conn, user):
        self.conn, self.user = conn, user

    def view(self, first, last):
        data = StudyData(self.conn, self.user['id'], subject_statuses=('draft','active','archived'))
        events = EventRepository(self.conn, self.user['id'])
        rows = events.events()
        changes = defaultdict(list)
        for row in events.exceptions([x['id'] for x in rows]):
            changes[row['event_id']].append(row)
        fixed = [item for row in rows for item in occurrences(row, changes[row['id']], first, last, self.user['timezone'])]
        study, summary = [], defaultdict(lambda: {'minutes': 0, 'count': 0})
        tz = ZoneInfo(self.user['timezone'])
        for task in data.tasks:
            row = task.row
            if not first <= row['scheduled_date'] <= last or row['status'] in ('carried_over', 'excluded'):
                continue
            subject = data.subject_by_id.get(row['subject_id'])
            if subject is None:
                continue
            item = data.task_view(task)
            item.update(subjectName=subject['name'], color=subject['color'], date=row['scheduled_date'].isoformat(),
                start=_local(row['starts_at'], tz).strftime('%H:%M') if row['starts_at'] else '',
                end=_local(row['ends_at'], tz).strftime('%H:%M') if row['ends_at'] else '',
                reason=row['placement_reason'] or '', moved=row['origin_task_id'] is not None)
            study.append(item)
            summary[row['subject_id']]['minutes'] += row['estimated_minutes']
            summary[row['subject_id']]['count'] += 1
        exams = [{'subjectId': s['id'], 'name': s['name'], 'examDate': s['exam_date'].isoformat(), 'color': s['color']}
            for s in data.subjects if s['exam_date'] and first <= s['exam_date'] <= last]
        return {'from': first.isoformat(), 'to': last.isoformat(), 'today': today_for(self.user).isoformat(),
            'exams': exams, 'fixed': sorted(fixed, key=lambda x: (x['date'],x['start'],x['id'])),
            'study': sorted(study, key=lambda x: (x['date'],x['start'],x['id'])), 'summary': dict(summary)}
