"""계획·기록 조회와 계산 (홈 S6, 기록 S7, 책 쌓기 S9).

계산 기준은 정의서 v2 4장이에요.
- 오늘 진행률 = 오늘 기록한 페이지 ÷ 오늘 계획 페이지 (보충 포함)
- 퀴즈 열림 = 전체 70% 이상, 또는 과목별 70% 이상이면 그 과목만 (4-4)
- 준비도 = 진도율 × 숙달도 (4-9). 진도율 = 끝낸 새 범위 페이지 ÷ 시험 범위 페이지,
  숙달도 = 퀴즈를 본 단원 점수의 페이지 가중 평균
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import Connection, func, select

from app.db import schema as t
from app.domain.progress import PageProgress
from app.services.task_views import TaskView, unit_no

OPEN_RATE = 70
STATUS_TO_FRONT = {"planned": "todo", "partial": "partial", "completed": "done", "missed": "none",
                   "carried_over": "none", "excluded": "none"}


@dataclass
class Task:
    row: dict
    ranges: list[dict] = field(default_factory=list)       # 계획 범위 (정렬)
    done_ranges: list[dict] = field(default_factory=list)  # 기록된 범위
    record: dict | None = None
    session_minutes: int | None = None
    pending_session: dict | None = None

    @property
    def id(self) -> str:
        return self.row["id"]

    @property
    def paged(self) -> list[dict]:
        return [r for r in self.ranges if r["start_page"] is not None and r["end_page"] is not None]

    @property
    def range_view(self) -> TaskView:
        code = self.ranges[0].get("unit_code") if len(self.ranges) == 1 else None
        return TaskView.from_ranges(self.ranges, self.done_ranges, unit_code=code)

    @property
    def first(self) -> int | None:
        return self.range_view.first

    @property
    def last(self) -> int | None:
        return self.range_view.last

    @property
    def pages(self) -> int:
        return self.range_view.pages

    @property
    def done_pages(self) -> int:
        return self.range_view.done_pages

    @property
    def done_to(self) -> int | None:
        return self.range_view.done_to


class StudyData:
    """한 사용자의 과목·단원·계획 태스크를 한 번에 읽어 두고 계산해요."""

    def __init__(self, conn: Connection, user_id: str):
        self.conn = conn
        self.user_id = user_id
        self.subjects = [dict(r) for r in conn.execute(
            select(t.subjects).where(t.subjects.c.user_id == user_id, t.subjects.c.status == "active")
            .order_by(t.subjects.c.exam_date)).mappings()]
        sids = [s["id"] for s in self.subjects]
        self.subject_by_id = {s["id"]: s for s in self.subjects}
        unit_rows = conn.execute(
            select(t.units, t.materials.c.subject_id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .where(t.materials.c.subject_id.in_(sids), t.units.c.retired_at.is_(None))
            .order_by(t.units.c.sort_order)).mappings()
        self.units = {r["id"]: dict(r) for r in unit_rows}
        self.plan = conn.execute(select(t.plans).where(t.plans.c.user_id == user_id)).mappings().first()

        self.tasks: list[Task] = []
        if not self.plan:
            return
        rows = conn.execute(
            select(t.study_tasks).join(t.subjects, t.study_tasks.c.subject_id == t.subjects.c.id)
            .where(t.study_tasks.c.plan_id == self.plan["id"], t.subjects.c.user_id == user_id)
            .order_by(t.study_tasks.c.scheduled_date, t.study_tasks.c.created_at)).mappings()
        by_id = {r["id"]: Task(dict(r)) for r in rows}
        self.tasks = list(by_id.values())
        if not by_id:
            return
        ids = list(by_id)
        # Historical range identity survives retirement/archival; the active
        # unit catalog above is only for the existing progress/quiz policies.
        for r in conn.execute(
            select(t.study_task_ranges, t.units.c.material_id, t.units.c.code.label("unit_code"))
            .join(t.units, t.study_task_ranges.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .join(t.study_tasks, t.study_task_ranges.c.task_id == t.study_tasks.c.id)
            .where(t.study_task_ranges.c.task_id.in_(ids), t.materials.c.subject_id == t.study_tasks.c.subject_id)
            .order_by(t.study_task_ranges.c.sort_order, t.study_task_ranges.c.id)).mappings():
            by_id[r["task_id"]].ranges.append(dict(r))
        records = {r["id"]: dict(r) for r in conn.execute(
            select(t.study_records).where(t.study_records.c.task_id.in_(ids))
            .order_by(t.study_records.c.recorded_at)).mappings()}
        for rec in records.values():
            by_id[rec["task_id"]].record = rec  # 태스크당 마지막 기록
        if records:
            for r in conn.execute(select(t.study_record_ranges)
                                  .where(t.study_record_ranges.c.record_id.in_(list(records)))
                                  .order_by(t.study_record_ranges.c.start_page)).mappings():
                task = by_id[records[r["record_id"]]["task_id"]]
                if task.record and task.record["id"] == r["record_id"]:
                    task.done_ranges.append(dict(r))
        sessions = conn.execute(select(t.study_sessions).where(t.study_sessions.c.task_id.in_(ids))).mappings()
        linked = {rec["session_id"] for rec in records.values() if rec["session_id"]}
        for s in sessions:
            task = by_id[s["task_id"]]
            minutes = round((s["measured_seconds"] or 0) / 60)
            if s["id"] in linked:
                task.session_minutes = (task.session_minutes or 0) + minutes
            elif s["status"] in ("ended", "auto_stopped"):
                task.pending_session = {"id": s["id"], "minutes": minutes}

    # ---------- 조회 ----------
    def tasks_on(self, day: date) -> list[Task]:
        tasks = [x for x in self.tasks if x.row["scheduled_date"] == day
                 and x.row["status"] not in ("carried_over", "excluded")]
        return sorted(tasks, key=lambda x: x.row["origin_task_id"] is None)  # 이월된 태스크가 맨 위

    def unit_of(self, task: Task) -> dict | None:
        return self.units.get(task.ranges[0]["unit_id"]) if task.ranges else None

    def quiz_done_on(self, subject_id: str, day: date) -> bool:
        return self.conn.execute(
            select(t.quiz_sessions.c.id).where(
                t.quiz_sessions.c.user_id == self.user_id, t.quiz_sessions.c.subject_id == subject_id,
                t.quiz_sessions.c.quiz_date == day, t.quiz_sessions.c.status == "submitted")).first() is not None

    def block_state(self, task: Task, today: date) -> str:
        """책 블록 상태 6종 (4-5): planned · doing · done(퀴즈 전) · stable · shaky · risk"""
        status = task.row["status"]
        if status == "partial":
            return "doing"
        if status != "completed":
            return "planned"
        unit = self.unit_of(task)
        rec_day = task.record["study_date"] if task.record else task.row["scheduled_date"]
        if unit is None or unit["mastery_level"] is None:
            return "done"
        if rec_day == today and not self.quiz_done_on(task.row["subject_id"], today):
            return "done"
        return unit["mastery_level"]

    # ---------- 계산 ----------
    def progress(self, day: date) -> dict:
        tasks = self.tasks_on(day)
        total = sum(x.pages for x in tasks)
        done = sum(x.done_pages for x in tasks)
        by_subject = defaultdict(lambda: [0, 0])
        for x in tasks:
            by_subject[x.row["subject_id"]][0] += x.done_pages
            by_subject[x.row["subject_id"]][1] += x.pages
        return {
            "doneCount": sum(1 for x in tasks if x.row["status"] == "completed"),
            "totalCount": len(tasks),
            "donePages": done,
            "totalPages": total,
            "rate": PageProgress(done, total).rate,
            "subjectRates": {sid: PageProgress(d, n).rate for sid, (d, n) in by_subject.items()},
            "recordedAll": all(x.row["status"] != "planned" for x in tasks) and bool(tasks),
        }

    def quiz_counts(self, day: date) -> dict[str, int]:
        """오늘의 퀴즈 문항 수. 이미 만든 오늘 퀴즈가 있으면 그 문항 수,
        없으면 '오늘 공부한 단원 × 5' (어제 오답은 퀴즈를 만들 때 더해져요)"""
        made = dict(self.conn.execute(
            select(t.quiz_sessions.c.subject_id, func.count(t.quiz_items.c.id))
            .join(t.quiz_items, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.quiz_sessions.c.user_id == self.user_id, t.quiz_sessions.c.quiz_date == day,
                   t.quiz_sessions.c.type == "daily")
            .group_by(t.quiz_sessions.c.subject_id)).all())
        units_by_subject = defaultdict(set)
        for x in self.tasks_on(day):
            unit = self.unit_of(x)
            if unit:
                units_by_subject[x.row["subject_id"]].add(unit["id"])
        return {sid: made.get(sid, 5 * len(u)) for sid, u in units_by_subject.items()}

    def quiz_view(self, day: date, prog: dict) -> dict:
        counts = self.quiz_counts(day)
        open_subjects = [{"subjectId": sid, "count": counts.get(sid, 0)}
                         for sid, r in prog["subjectRates"].items() if r >= OPEN_RATE]
        return {
            "openRate": OPEN_RATE,
            "open": prog["rate"] >= OPEN_RATE,
            "remainPages": max(0, math.ceil(prog["totalPages"] * OPEN_RATE / 100) - prog["donePages"]),
            "openSubjects": open_subjects,
            "totalCount": sum(counts.values()),
        }

    def readiness(self, subject: dict) -> dict:
        units = [u for u in self.units.values() if u["subject_id"] == subject["id"] and u["in_scope"]
                 and u["start_page"] is not None]
        scope = sum(u["end_page"] - u["start_page"] + 1 for u in units)
        unit_ids = {u["id"] for u in units}
        studied: dict[str, set[int]] = defaultdict(set)
        for x in self.tasks:
            if x.row["subject_id"] != subject["id"] or x.row["task_type"] != "study":
                continue
            for r in x.done_ranges:
                if r["unit_id"] in unit_ids and r["start_page"] is not None:
                    studied[r["unit_id"]].update(range(r["start_page"], r["end_page"] + 1))
        studied_pages = sum(len(v) for v in studied.values())
        progress = studied_pages / scope if scope else 0
        quizzed = [u for u in units if u["mastery_score"] is not None]
        weight = sum(u["end_page"] - u["start_page"] + 1 for u in quizzed)
        mastery = (sum(u["mastery_score"] * (u["end_page"] - u["start_page"] + 1) for u in quizzed) / weight / 100
                   if weight else None)
        return {
            "subjectId": subject["id"],
            "readiness": round(progress * mastery * 100) if mastery is not None else 0,
            "target": subject["target_score"],
            "targetLabel": subject["target_grade"],
            "progressRate": round(progress * 100, 1),
            "mastery": round(mastery * 100, 1) if mastery is not None else None,
            "studiedPages": studied_pages,
            "totalPages": scope,
        }

    def task_view(self, x: Task) -> dict:
        return {
            "id": x.id,
            "subjectId": x.row["subject_id"],
            "title": x.row["title"],
            **x.range_view.as_dict(),
            "minutes": x.row["estimated_minutes"],
            "reason": x.row["placement_reason"],
            "status": STATUS_TO_FRONT.get(x.row["status"], "todo"),
            "carried": x.row["origin_task_id"] is not None,
            "supplement": x.row["task_type"] == "remedial",
            "measuredMinutes": x.session_minutes,
            "pendingSessionId": x.pending_session["id"] if x.pending_session else None,
            "pendingMinutes": x.pending_session["minutes"] if x.pending_session else None,
        }

    def recent_days(self, today: date) -> list[dict]:
        days = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            tasks = self.tasks_on(day)
            if day < today:
                tasks = [x for x in tasks if x.row["status"] in ("completed", "partial")]
            days.append({"date": day.isoformat(),
                         "blocks": [{"state": self.block_state(x, today),
                                     "taskId": x.id if day == today else None} for x in tasks]})
        return days
