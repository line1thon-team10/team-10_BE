"""Read-only monthly facts; placement totals and unique study pages stay separate."""
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.core.clock import now_utc
from app.db import schema as t
from app.domain.progress import PageProgress, rounded_percent
from app.services.monthly import MonthlyService, month_bounds, new_pages
from app.services.task_views import unit_no


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class MonthlyReportService(MonthlyService):
    def report(self, month):
        first, last = month_bounds(month)
        until = min(self.today, last)
        result = {'month': month, 'from': first.isoformat(), 'until': until.isoformat(),
            'studyDays': 0, 'plannedDays': 0, 'achievement': {'done': 0, 'plan': 0, 'rate': 0},
            'time': {'measuredMinutes': 0, 'plannedMinutes': 0},
            'quiz': {'total': 0, 'wrong': 0, 'unsureRight': 0}, 'subjects': [], 'grades': []}
        if until < first:
            return result

        totals = {first + timedelta(days=offset): self.totals.view(first + timedelta(days=offset),data=self.data,visibility=self.visibility)
            for offset in range((until-first).days+1)}
        plan = sum(row['plan'] for row in totals.values())
        done = sum(row['done'] for row in totals.values())
        result['achievement'] = {'plan': plan, 'done': done, 'rate': PageProgress(done, plan).rate}
        planned_days = {day for day, row in totals.items()
            if row['plan'] or any(subject['unpagedMinutes'] for subject in row['subjects'])}
        study_days = set()
        planned_minutes = 0
        for task in self.data.tasks:
            day = task.row['scheduled_date']
            if first <= day <= until:
                frozen = any(row['subjectId'] == task.row['subject_id'] and row['finalized']
                    for row in totals[day]['subjects'])
                if self.visibility.task(task) and task.row['status'] != 'excluded' and (frozen or task.row['status'] != 'carried_over'):
                    planned_minutes += task.row['estimated_minutes'] or 0
                    planned_days.add(day)
            if task.record and task.record['result'] in ('completed', 'partial'):
                record_day = task.record['study_date']
                if first <= record_day <= until and (task.done_pages or not task.paged):
                    study_days.add(record_day)
        seconds, measured_days = self._measured(first, until)
        result['time'] = {'measuredMinutes': int(seconds // 60), 'plannedMinutes': planned_minutes}
        attempts = self._attempts(first, until)
        study_days.update(row['answered_date'] for row in attempts)
        study_days.update(measured_days)
        result['studyDays'], result['plannedDays'] = len(study_days), len(planned_days)
        result['quiz'] = {'total': len(attempts), 'wrong': sum(not row['is_correct'] for row in attempts),
            'unsureRight': sum(row['is_correct'] and row['confidence'] == 'unsure' for row in attempts)}
        pages = new_pages(self.data.tasks, first, until)
        total_pages = sum(pages.values())
        result['subjects'] = [{'subjectId': sid, 'pages': count,
            'share': rounded_percent(Fraction(count, total_pages), places=1) if total_pages else 0}
            for sid, count in sorted(pages.items()) if count]
        result['grades'] = self._grades(first, until)
        return result

    def _measured(self, first, until):
        """Clip closed focus intervals, excluding pauses and not-yet-measured time."""
        rows = self.conn.execute(select(t.study_sessions)
            .join(t.study_tasks, t.study_sessions.c.task_id == t.study_tasks.c.id)
            .join(t.plans, t.study_tasks.c.plan_id == t.plans.c.id)
            .join(t.subjects, t.study_tasks.c.subject_id == t.subjects.c.id)
            .where(t.study_sessions.c.user_id == self.user['id'], t.plans.c.user_id == self.user['id'],
                t.subjects.c.user_id == self.user['id'])).mappings().all()
        segments = defaultdict(list)
        for row in self.conn.execute(select(t.study_session_segments).where(
                t.study_session_segments.c.session_id.in_([row['id'] for row in rows]))).mappings():
            segments[row['session_id']].append(row)
        zone = ZoneInfo(self.user.get('timezone') or 'Asia/Seoul')
        lower = datetime.combine(first, time.min, zone).astimezone(timezone.utc)
        upper = datetime.combine(until + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        now = now_utc()
        seconds, days = 0, set()
        for row in rows:
            if self.user.get('demo_date') is not None:
                if not first <= row['study_date'] <= until:
                    continue
                measured = sum(max(0, (min(aware(segment['ended_at']), now)-aware(segment['started_at'])).total_seconds())
                    for segment in segments[row['id']] if segment['ended_at'] is not None)
                if not segments[row['id']] and row['status'] in ('ended', 'auto_stopped'):
                    measured = max(0, row['measured_seconds'] or 0)
                if measured:
                    seconds += measured
                    days.add(row['study_date'])
                continue
            for segment in segments[row['id']]:
                if segment['ended_at'] is None:
                    continue
                start, end = max(lower, aware(segment['started_at'])), min(upper, now, aware(segment['ended_at']))
                if end <= start:
                    continue
                seconds += (end-start).total_seconds()
                day, final_day = start.astimezone(zone).date(), (end-timedelta(microseconds=1)).astimezone(zone).date()
                while day <= final_day:
                    days.add(day)
                    day += timedelta(days=1)
            # Legacy sessions without segments have no recoverable split points.
            if not segments[row['id']] and row['status'] in ('ended', 'auto_stopped') \
                    and first <= row['study_date'] <= until and (row['measured_seconds'] or 0) > 0:
                seconds += row['measured_seconds']
                days.add(row['study_date'])
        return seconds, days

    def _attempts(self, first, until):
        return self.conn.execute(select(t.quiz_attempts)
            .join(t.quiz_items, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
            .join(t.quiz_sessions, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .join(t.questions, t.quiz_items.c.question_id == t.questions.c.id)
            .join(t.units, t.questions.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
            .where(t.subjects.c.user_id == self.user['id'], t.quiz_sessions.c.user_id == self.user['id'],
                or_(t.quiz_sessions.c.subject_id.is_(None), t.quiz_sessions.c.subject_id == t.subjects.c.id),
                t.quiz_sessions.c.status == 'submitted', t.quiz_sessions.c.finalized_at.is_not(None),
                t.quiz_sessions.c.grading_status == 'completed', t.quiz_attempts.c.finalized_at.is_not(None),
                t.quiz_attempts.c.graded_at.is_not(None), t.quiz_attempts.c.grading_status.in_(('graded', 'fallback')),
                t.quiz_attempts.c.is_correct.is_not(None), t.quiz_attempts.c.answered_date.between(first, until))).mappings().all()

    def _grades(self, first, until):
        rows = self.conn.execute(select(t.mastery_history, t.units.c.material_id,
                t.units.c.code, t.units.c.title, t.materials.c.subject_id)
            .join(t.units, t.mastery_history.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
            .join(t.quiz_sessions, t.mastery_history.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.subjects.c.user_id == self.user['id'], t.quiz_sessions.c.user_id == self.user['id'],
                or_(t.quiz_sessions.c.subject_id.is_(None), t.quiz_sessions.c.subject_id == t.subjects.c.id),
                t.quiz_sessions.c.status == 'submitted', t.quiz_sessions.c.finalized_at.is_not(None),
                t.quiz_sessions.c.grading_status == 'completed', t.mastery_history.c.recorded_date <= until)
            .order_by(t.mastery_history.c.recorded_date, t.mastery_history.c.created_at, t.mastery_history.c.id)).mappings()
        before, after = {}, {}
        for row in rows:
            after[row['unit_id']] = row
            if row['recorded_date'] < first:
                before[row['unit_id']] = row
        return [{'subjectId': row['subject_id'], 'materialId': row['material_id'], 'unitId': uid,
            'unitNo': unit_no(row['code']), 'title': row['title'],
            'before': {'grade': before[uid]['level'], 'score': before[uid]['score']} if uid in before else None,
            'after': {'grade': row['level'], 'score': row['score']}}
            for uid, row in sorted(after.items(), key=lambda pair: (pair[1]['subject_id'], pair[1]['material_id'], pair[0]))]
