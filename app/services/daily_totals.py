"""Mutable daily planning totals become immutable denominators before day close."""
from collections import defaultdict

from sqlalchemy import select, update

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.progress import PageProgress
from app.domain.ranges import PageSpan, page_count
from app.services.records import lock_record_user, record_date
from app.services.study import StudyData


class DailyTotalsService:
    def __init__(self, conn, user):
        self.conn, self.user = conn, user

    def _data(self):
        return StudyData(self.conn,self.user['id'],subject_statuses=('draft','active','archived'))

    def _stored(self, day):
        return {row['subject_id']:dict(row) for row in self.conn.execute(select(t.daily_plan_totals).where(
            t.daily_plan_totals.c.user_id==self.user['id'],t.daily_plan_totals.c.study_date==day)).mappings()}

    @staticmethod
    def _current(data,day):
        grouped=defaultdict(lambda:{'planned_new_pages':0,'planned_total_pages':0,'planned_unpaged_minutes':0})
        new_spans=defaultdict(list)
        for task in data.tasks_on(day):
            row=grouped[task.row['subject_id']]
            row['planned_total_pages']+=task.pages
            if task.row['task_type']=='study':
                new_spans[task.row['subject_id']].extend(
                    PageSpan(part['material_id'],part['start_page'],part['end_page']) for part in task.paged)
            if not task.paged:
                row['planned_unpaged_minutes']+=task.row['estimated_minutes'] or 0
        for sid,spans in new_spans.items():
            grouped[sid]['planned_new_pages']=page_count(spans)
        return grouped

    def snapshot(self,day,*,finalize=False,at=None):
        """Finalize before moving/excluding placements, in their same transaction."""
        at=at or now_utc()
        self.user=lock_record_user(self.conn,self.user)
        if finalize and day>record_date(self.user,at):
            raise ApiError('INVALID_INPUT','미래 날짜를 마감할 수 없어요',422)
        data=self._data()
        if data.plan is None:
            return {}
        stored,current=self._stored(day),self._current(data,day)
        for sid in current.keys() | stored.keys():
            previous=stored.get(sid)
            if previous and previous['finalized_at'] is not None:
                continue
            values={**current[sid],'plan_id':data.plan['id'],'updated_at':at,
                    'finalized_at':at if finalize else None}
            identity={'user_id':self.user['id'],'subject_id':sid,'study_date':day}
            if previous:
                self.conn.execute(update(t.daily_plan_totals).filter_by(**identity).where(
                    t.daily_plan_totals.c.finalized_at.is_(None)).values(**values))
            else:
                self.conn.execute(t.daily_plan_totals.insert().values(**identity,**values))
        return self._stored(day)

    def view(self,day,*,at=None):
        """Corrections change only numerators; future completion is never predicted."""
        at=at or now_utc()
        data=self._data()
        stored,current=self._stored(day),self._current(data,day)
        done=defaultdict(int)
        if day<=record_date(self.user,at):
            for task in data.tasks:
                if task.row['scheduled_date']==day:
                    frozen=stored.get(task.row['subject_id'],{}).get('finalized_at') is not None
                    if task.row['status'] != 'excluded' and (frozen or task.row['status'] != 'carried_over'):
                        done[task.row['subject_id']]+=task.done_pages
        subjects=[]
        for sid in sorted(current.keys() | stored.keys()):
            row=stored.get(sid)
            frozen=row is not None and row['finalized_at'] is not None
            values=row if frozen else current[sid]
            plan=values['planned_total_pages']
            completed=min(done[sid],plan)
            subjects.append({'subjectId':sid,'plan':plan,'done':completed,
                'rate':PageProgress(completed,plan).rate,'newPages':values['planned_new_pages'],
                'unpagedMinutes':values['planned_unpaged_minutes'],'finalized':frozen})
        plan=sum(row['plan'] for row in subjects)
        completed=sum(row['done'] for row in subjects)
        return {'date':day.isoformat(),'plan':plan,'done':completed,
                'rate':PageProgress(completed,plan).rate,'subjects':subjects}
