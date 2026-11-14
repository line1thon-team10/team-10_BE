"""Monthly placement achievement and distinct new-study page projections."""
import calendar
from collections import defaultdict
from datetime import date, timedelta

from app.core.clock import today_for
from app.domain.ranges import PageSpan, intersect, page_count
from app.services.daily_totals import DailyTotalsService
from app.services.study import StudyData


def month_bounds(month):
    first = date.fromisoformat(month + '-01')
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def new_pages(tasks, first, last):
    spans = defaultdict(list)
    for task in tasks:
        if task.row['task_type'] != 'study' or not first <= task.row['scheduled_date'] <= last:
            continue
        for planned in task.paged:
            done = [PageSpan(planned['material_id'], row['start_page'], row['end_page'])
                for row in task.done_ranges if row['unit_id']==planned['unit_id']
                and row['start_page'] is not None and row['end_page'] is not None]
            spans[task.row['subject_id']].extend(intersect(
                [PageSpan(planned['material_id'], planned['start_page'], planned['end_page'])], done))
    return {sid: page_count(values) for sid, values in spans.items()}


class MonthlyService:
    def __init__(self, conn, user):
        self.conn, self.user = conn, user
        self.today = today_for(user)
        self.data = StudyData(conn, user['id'], subject_statuses=('draft','active','archived'))
        self.totals = DailyTotalsService(conn, user)

    def view(self, month):
        first, last = month_bounds(month)
        start = first - timedelta(days=first.weekday())
        end = last + timedelta(days=6-last.weekday())
        days = []
        for offset in range((end-start).days+1):
            day = start + timedelta(days=offset)
            totals = self.totals.view(day)
            tasks = self.data.tasks_on(day)
            pages = new_pages(self.data.tasks, day, day) if day <= self.today else {}
            labels = []
            if any(task.row['task_type']=='review' for task in tasks): labels.append('총복습')
            if any(s['exam_date']==day for s in self.data.subjects): labels.append('시험')
            planned = totals['plan'] > 0 or any(s['unpagedMinutes'] for s in totals['subjects'])
            measured = any(task.session_minutes is not None or task.pending_session is not None for task in tasks)
            days.append({'date':day.isoformat(), 'inMonth':first<=day<=last, 'today':day==self.today,
                'future':day>self.today, 'planned':planned, 'plan':totals['plan'], 'done':totals['done'], 'rate':totals['rate'],
                'bySubject':[{'subjectId':sid,'pages':value} for sid,value in sorted(pages.items())],
                'labels':labels, 'estimated':planned and not measured})
        return {'month':month,'today':self.today.isoformat(),'note':None,
            'subjects':[{'id':s['id'],'name':s['name'],'color':s['color']} for s in self.data.subjects], 'days':days}
