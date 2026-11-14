"""Persistent books and dated placement projections share evidence-based states."""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.db import schema as t
from app.domain.ranges import PageSpan, intersect, page_count


class BookViews:
    def __init__(self, data, today):
        self.data, self.today = data, today
        self.tasks = defaultdict(list)
        for task in data.tasks:
            if task.row["subject_id"] in data.subject_by_id:
                self.tasks[task.row["block_id"]].append(task)
        self.blocks = {row["id"]: dict(row) for row in data.conn.execute(select(t.study_blocks).where(
            t.study_blocks.c.id.in_(self.tasks),
            t.study_blocks.c.subject_id.in_(data.subject_by_id))).mappings()}
        # Historical unit identities remain visible only when a book references them.
        self.units = dict(data.units)
        historical = {row["unit_id"] for row in self.blocks.values()} - set(self.units) - {None}
        for row in data.conn.execute(select(t.units, t.materials.c.subject_id)
                .join(t.materials, t.materials.c.id == t.units.c.material_id)
                .where(t.units.c.id.in_(historical), t.materials.c.subject_id.in_(data.subject_by_id))).mappings():
            self.units[row["id"]] = dict(row)
        self.finalized_today = set(data.conn.scalars(select(t.questions.c.unit_id)
            .join(t.quiz_items, t.quiz_items.c.question_id == t.questions.c.id)
            .join(t.quiz_sessions, t.quiz_sessions.c.id == t.quiz_items.c.quiz_session_id)
            .where(t.quiz_sessions.c.user_id == data.user_id, t.quiz_sessions.c.quiz_date == today,
                   t.quiz_sessions.c.status == "submitted", t.quiz_sessions.c.grading_status == "completed",
                   t.quiz_sessions.c.finalized_at.is_not(None))))

    def ordered(self, block_id):
        return sorted(self.tasks[block_id], key=lambda task: (
            task.row["scheduled_date"], task.row["created_at"], task.id))

    def representative(self, block_id, day=None):
        tasks = self.ordered(block_id)
        if day is not None:
            tasks = [task for task in tasks if task.row["scheduled_date"] == day]
        active = [task for task in tasks if task.row["status"] not in ("excluded", "carried_over")]
        recorded = [task for task in tasks if task.record and task.record["result"] in ("partial", "completed")]
        return (active or recorded or [None])[-1]

    def state(self, block_id, *, through=None):
        tasks = self.ordered(block_id)
        ids = {task.id for task in tasks}
        roots = [task for task in tasks if task.row["origin_task_id"] not in ids]
        # Root ranges describe the whole durable book, not merely a carried tail.
        required = [PageSpan(row["unit_id"], row["start_page"], row["end_page"])
                    for task in roots for row in task.paged]
        evidence = [task for task in tasks if task.record and task.record["result"] in ("partial", "completed")
                    and (through is None or task.record["study_date"] <= through)]
        actual = [PageSpan(row["unit_id"], row["start_page"], row["end_page"])
                  for task in evidence for row in task.done_ranges
                  if row["start_page"] is not None and row["end_page"] is not None]
        if required:
            done = page_count(intersect(required, actual))
            if done == 0:
                return "planned"
            if done < page_count(required):
                return "doing"
        elif not any(task.record["result"] == "completed" for task in evidence):
            return "doing" if evidence else "planned"
        unit = self.units.get(self.blocks[block_id]["unit_id"])
        if not unit or not unit["mastery_level"]:
            return "done"
        completed_day = max(task.record["study_date"] for task in evidence)
        if completed_day == self.today and unit["id"] not in self.finalized_today:
            return "done"
        return unit["mastery_level"]

    def by_unit(self):
        result = defaultdict(list)
        for block_id in sorted(self.blocks, key=lambda value: (
                self.ordered(value)[0].row["scheduled_date"],
                self.ordered(value)[0].row["created_at"], value)):
            block = self.blocks[block_id]
            unit = self.units.get(block["unit_id"])
            task = self.representative(block_id)
            if task is None or unit is None or unit["subject_id"] != block["subject_id"]:
                continue
            result[unit["id"]].append({"id": block_id, "state": self.state(block_id),
                "supplement": block["task_type"] == "remedial",
                "task": {"id": task.id, "title": task.row["title"], **task.range_view.as_dict()}})
        return result

    def recent_days(self):
        days = []
        for i in range(6, -1, -1):
            day = self.today - timedelta(days=i)
            blocks = []
            for block_id in self.blocks:
                task = self.representative(block_id, day)
                if task is None:
                    continue
                if day < self.today and not any(
                        item.record and item.record["result"] in ("completed", "partial")
                        and item.row["scheduled_date"] == day for item in self.tasks[block_id]):
                    continue
                blocks.append({"id": block_id, "state": self.state(block_id, through=day),
                               "taskId": task.id if day == self.today else None})
            days.append({"date": day.isoformat(), "blocks": blocks})
        return days
