"""Record writes share the caller's user lock and request transaction."""
from uuid import uuid4

from sqlalchemy import select, update

from app.db import schema as t


class RecordRepository:
    def __init__(self, connection):
        self.conn = connection

    def session(self, session_id):
        return self.conn.execute(select(t.study_sessions).where(
            t.study_sessions.c.id == session_id)).mappings().first()

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
        self.conn.execute(update(t.study_tasks).where(
            t.study_tasks.c.id == task.id).values(status=status))
        if status == "completed":
            self.conn.execute(update(t.study_blocks).where(
                t.study_blocks.c.id == task.row["block_id"],
                t.study_blocks.c.stacked_at.is_(None)).values(stacked_at=at))
        return record_id
