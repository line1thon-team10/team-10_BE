"""계획·기록 조회와 계산 (홈 S6, 기록 S7, 책 쌓기 S9).

계산 기준은 정의서 v2 4장이에요.
- 오늘 달성률 = 배치별 실제 완료 페이지 ÷ 배치별 계획 페이지 (보충 포함)
- 퀴즈 진행률 = 오늘 출제 가능한 자료별 페이지 합집합의 완료 비율
- 퀴즈 열림 = 원시 전체 비율 70% 이상, 또는 과목별 70% 이상이면 그 과목만 (4-4)
- 준비도 = 진도율 × 숙달도 (4-9). 진도율 = 끝낸 새 범위 페이지 ÷ 시험 범위 페이지,
  숙달도 = 퀴즈를 본 단원 점수의 페이지 가중 평균
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

from sqlalchemy import Connection, func, select

from app.db import schema as t
from app.domain.progress import PageProgress, rounded_percent
from app.domain.ranges import PageSpan, intersect, normalize, page_count
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

    def __init__(self, conn: Connection, user_id: str, *, subject_statuses=("active",)):
        self.conn = conn
        self.user_id = user_id
        self.subjects = [dict(r) for r in conn.execute(
            select(t.subjects).where(t.subjects.c.user_id == user_id, t.subjects.c.status.in_(subject_statuses))
            .order_by(t.subjects.c.exam_date, t.subjects.c.created_at, t.subjects.c.id)).mappings()]
        sids = [s["id"] for s in self.subjects]
        self.subject_by_id = {s["id"]: s for s in self.subjects}
        unit_rows = conn.execute(
            select(t.units, t.materials.c.subject_id, t.materials.c.quiz_enabled)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .where(t.materials.c.subject_id.in_(sids), t.units.c.retired_at.is_(None))
            .order_by(t.units.c.sort_order)).mappings()
        self.units = {r["id"]: dict(r) for r in unit_rows}
        concepts = defaultdict(list)
        for row in conn.execute(select(t.concepts).where(
                t.concepts.c.unit_id.in_(list(self.units)), t.concepts.c.retired_at.is_(None))).mappings():
            concepts[row["unit_id"]].append(row)
        self.unit_scopes = {uid: self._unit_scope(unit, concepts[uid]) for uid, unit in self.units.items()}
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
        tasks = [x for x in self.tasks if x.row["subject_id"] in self.subject_by_id and x.row["scheduled_date"] == day
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
    @staticmethod
    def _unit_scope(unit: dict, concepts: list) -> tuple[PageSpan, ...]:
        """Current included concept union, or the whole unit when no concepts exist."""
        start, end = unit["start_page"], unit["end_page"]
        if not unit["in_scope"] or unit["quantity_basis"] != "page" or start is None or end is None or not 1 <= start <= end:
            return ()
        whole = (PageSpan(unit["material_id"], start, end),)
        if not concepts:
            return whole
        return intersect(whole, (PageSpan(unit["material_id"], c["start_page"], c["end_page"])
                                 for c in concepts if c["in_scope"]))

    def _task_exam_ranges(self, task: Task, *, quiz_only: bool = False) -> tuple[tuple[PageSpan, ...], tuple[PageSpan, ...]]:
        """Clip this placement and its latest unit-owned evidence to current scope."""
        by_unit = defaultdict(list)
        for row in task.done_ranges:
            if row["start_page"] is not None and row["end_page"] is not None:
                by_unit[row["unit_id"]].append(PageSpan(row["unit_id"], row["start_page"], row["end_page"]))
        planned, completed = [], []
        for row in task.paged:
            unit = self.units.get(row["unit_id"])
            if unit is None or (quiz_only and not unit["quiz_enabled"]):
                continue
            scope = intersect(self.unit_scopes[unit["id"]],
                              (PageSpan(unit["material_id"], row["start_page"], row["end_page"]),))
            planned.extend(scope)
            for span in scope:
                matches = intersect((PageSpan(unit["id"], span.start, span.end),), by_unit[unit["id"]])
                completed.extend(PageSpan(span.material_id, match.start, match.end) for match in matches)
        return normalize(planned), normalize(completed)

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
        없으면 '오늘 출제 가능한 단원 × 5' (어제 오답은 퀴즈를 만들 때 더해져요)"""
        made = dict(self.conn.execute(
            select(t.quiz_sessions.c.subject_id, func.count(t.quiz_items.c.id))
            .join(t.quiz_items, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.quiz_sessions.c.user_id == self.user_id, t.quiz_sessions.c.quiz_date == day,
                   t.quiz_sessions.c.type == "daily")
            .group_by(t.quiz_sessions.c.subject_id)).all())
        units_by_subject = defaultdict(set)
        for x in self.tasks_on(day):
            for row in x.paged:
                unit = self.units.get(row["unit_id"])
                if unit and unit["quiz_enabled"] and intersect(self.unit_scopes[unit["id"]],
                        (PageSpan(unit["material_id"], row["start_page"], row["end_page"]),)):
                    units_by_subject[x.row["subject_id"]].add(unit["id"])
        return {sid: made.get(sid, 5 * len(u)) for sid, u in units_by_subject.items()}

    def quiz_progress(self, day: date) -> dict:
        """Today's eligible PDF union, using only these placements' own records."""
        by_subject = defaultdict(lambda: [[], []])
        for task in self.tasks_on(day):
            sid = task.row["subject_id"]
            if sid not in self.subject_by_id:
                continue
            planned, completed = self._task_exam_ranges(task, quiz_only=True)
            by_subject[sid][0].extend(planned)
            by_subject[sid][1].extend(completed)
        subjects = {sid: PageProgress.from_ranges(planned, completed)
                    for sid, (planned, completed) in by_subject.items()}
        total = PageProgress.from_ranges(
            (span for planned, _ in by_subject.values() for span in planned),
            (span for _, completed in by_subject.values() for span in completed))
        opened = total.reaches(OPEN_RATE)
        return {
            "donePages": total.done_pages, "totalPages": total.total_pages, "rate": total.rate,
            "subjectRates": {sid: progress.rate for sid, progress in subjects.items()},
            "open": opened,
            "subjectOpen": {sid: progress.total_pages > 0 and (opened or progress.reaches(OPEN_RATE))
                            for sid, progress in subjects.items()},
            "remainPages": total.remaining_pages(OPEN_RATE),
        }

    def quiz_view(self, day: date, prog: dict) -> dict:
        # Keep the existing caller signature; home placement achievement is not
        # a quiz denominator and must never decide this view's eligibility.
        prog = self.quiz_progress(day)
        counts = self.quiz_counts(day)
        open_subjects = [{"subjectId": sid, "count": counts.get(sid, 0)}
                         for sid, opened in prog["subjectOpen"].items() if opened]
        return {
            "openRate": OPEN_RATE,
            "open": prog["open"],
            "remainPages": prog["remainPages"],
            "openSubjects": open_subjects,
            "totalCount": sum(counts.values()),
        }

    def readiness(self, subject: dict) -> dict:
        units = [u for u in self.units.values() if u["subject_id"] == subject["id"] and self.unit_scopes[u["id"]]]
        scope = normalize(span for unit in units for span in self.unit_scopes[unit["id"]])
        studied = []
        for x in self.tasks:
            if x.row["subject_id"] != subject["id"] or x.row["task_type"] != "study":
                continue
            _, completed = self._task_exam_ranges(x)
            studied.extend(completed)
        progress = PageProgress.from_ranges(scope, studied)
        # Overlapping concepts within a unit are defined; overlapping current
        # units in one PDF have no authoritative score weighting (legacy damage).
        ambiguous = sum(page_count(self.unit_scopes[u["id"]]) for u in units) != progress.total_pages
        quizzed = [u for u in units if u["quiz_enabled"] and u["mastery_score"] is not None]
        weight = sum(page_count(self.unit_scopes[u["id"]]) for u in quizzed)
        mastery = (sum(Fraction(str(u["mastery_score"])) * page_count(self.unit_scopes[u["id"]]) for u in quizzed)
                   / weight / 100 if weight and not ambiguous else None)
        return {
            "subjectId": subject["id"],
            "readiness": rounded_percent(progress.ratio * mastery) if mastery is not None else 0,
            "target": subject["target_score"],
            "targetLabel": subject["target_grade"],
            "progressRate": rounded_percent(progress.ratio, places=1),
            "mastery": rounded_percent(mastery, places=1) if mastery is not None else None,
            "studiedPages": progress.done_pages,
            "totalPages": progress.total_pages,
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
        from app.services.book_views import BookViews
        return BookViews(self, today).recent_days()
