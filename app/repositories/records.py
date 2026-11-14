"""Record writes share the caller's user lock and request transaction."""
from uuid import uuid4

from sqlalchemy import select, update

from app.db import schema as t


class RecordRepository:
    def __init__(self, connection):
        self.conn = connection

    def task_context(self, task_id, user_id):
        return self.conn.execute(select(t.study_tasks.c.id, t.subjects.c.name.label("subject_name"))
            .join(t.subjects, t.study_tasks.c.subject_id == t.subjects.c.id)
            .join(t.plans, t.study_tasks.c.plan_id == t.plans.c.id)
            .where(t.study_tasks.c.id == task_id, t.subjects.c.user_id == user_id,
                   t.plans.c.user_id == user_id)).mappings().first()

    def ranges(self, task_id):
        return [dict(row) for row in self.conn.execute(
            select(t.study_task_ranges, t.units.c.material_id,
                   t.materials.c.subject_id.label("range_subject_id"),
                   t.concepts.c.unit_id.label("concept_unit_id"))
            .join(t.units, t.study_task_ranges.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .outerjoin(t.concepts, t.study_task_ranges.c.concept_id == t.concepts.c.id)
            .where(t.study_task_ranges.c.task_id == task_id)
            .order_by(t.study_task_ranges.c.sort_order, t.study_task_ranges.c.id)).mappings()]

    def session(self, session_id, user_id):
        return self.conn.execute(select(t.study_sessions).where(
            t.study_sessions.c.id == session_id,
            t.study_sessions.c.user_id == user_id)).mappings().first()

    def session_record(self, session_id):
        return self.conn.scalar(select(t.study_records.c.id).where(
            t.study_records.c.session_id == session_id))

    def replace(self, task, result, status, ranges, study_date, session_id, at):
        if task.record:
            record_id = task.record["id"]
            values = {"result": result, "updated_at": at}
            if session_id:
                values["session_id"] = session_id
            self.conn.execute(update(t.study_records).where(
                t.study_records.c.id == record_id).values(**values))
            self.conn.execute(t.study_record_ranges.delete().where(
                t.study_record_ranges.c.record_id == record_id))
        else:
            record_id = str(uuid4())
            self.conn.execute(t.study_records.insert().values(
                id=record_id, task_id=task.id, session_id=session_id, result=result,
                recorded_at=at, study_date=study_date, updated_at=at))
        for item in ranges:
            self.conn.execute(t.study_record_ranges.insert().values(
                id=str(uuid4()), record_id=record_id, unit_id=item["unit_id"],
                start_page=item["start_page"], end_page=item["end_page"]))
        # Record corrections change learning evidence, not retired placements.
        self.conn.execute(update(t.study_tasks).where(
            t.study_tasks.c.id == task.id,
            t.study_tasks.c.status.not_in(("carried_over", "excluded"))).values(status=status))
        if status == "completed":
            self.conn.execute(update(t.study_blocks).where(
                t.study_blocks.c.id == task.row["block_id"],
                t.study_blocks.c.stacked_at.is_(None)).values(stacked_at=at))
        return record_id
