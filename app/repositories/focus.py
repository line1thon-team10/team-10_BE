"""집중 조회는 보관된 과목의 기존 세션도 소유자에게 제공한다."""
from sqlalchemy import select

from app.db import schema as t


class FocusRepository:
    def __init__(self, connection, user_id):
        self.conn, self.user_id = connection, user_id

    def task(self, task_id):
        row = self.conn.execute(select(t.study_tasks, t.subjects.c.name.label("subject_name"))
            .join(t.subjects,t.study_tasks.c.subject_id==t.subjects.c.id)
            .join(t.plans,t.study_tasks.c.plan_id==t.plans.c.id)
            .where(t.study_tasks.c.id==task_id,t.subjects.c.user_id==self.user_id,t.plans.c.user_id==self.user_id)).mappings().first()
        return dict(row) if row else None

    def ranges(self, task):
        rows = self.conn.execute(select(t.study_task_ranges, t.units.c.material_id)
            .join(t.units,t.study_task_ranges.c.unit_id==t.units.c.id)
            .join(t.materials,t.units.c.material_id==t.materials.c.id)
            .where(t.study_task_ranges.c.task_id==task["id"],t.materials.c.subject_id==task["subject_id"])
            .order_by(t.study_task_ranges.c.sort_order)).mappings()
        return [dict(row) for row in rows]

    def record_ranges(self, task_id):
        record = self.conn.execute(select(t.study_records.c.id).where(t.study_records.c.task_id==task_id)
                                   .order_by(t.study_records.c.recorded_at.desc()).limit(1)).scalar()
        if record is None:
            return []
        return list(self.conn.execute(select(t.study_record_ranges).where(t.study_record_ranges.c.record_id==record)).mappings())

    def all_segments(self):
        return list(self.conn.execute(select(t.study_session_segments, t.study_sessions.c.study_date,
            t.study_tasks.c.subject_id).join(t.study_sessions,t.study_session_segments.c.session_id==t.study_sessions.c.id)
            .join(t.study_tasks,t.study_sessions.c.task_id==t.study_tasks.c.id)
            .where(t.study_sessions.c.user_id==self.user_id)).mappings())
