"""자정 판정 · 이월 · 분배안 (백엔드 A 영역: RC-01~05, HM-05, NF-08).

규칙 (정의서 v2 4-8, 4-12)
- 판정은 하루가 끝날 때(데모는 [다음 날로]). 단위는 페이지
- 과목별 밀린 페이지 ≥ 그 과목 하루 평균 계획 페이지의 50%(올림) → 분배안 3개, 미만이면 이월
- 하루 평균 = 그 과목 시험 범위 페이지 ÷ 그 과목 태스크가 있는 날 수
- 이월 = 남은 범위를 그 과목 다음 공부일의 첫 태스크로
- 보충 학습은 어떤 경우에도 빼거나 미루지 않아요 (분배안 대상이 아니라 늘 이월)
- 모든 계획 변경은 사용자가 고른 뒤에만 적용 (분배안 [적용])
"""
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Connection, select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.services.study import StudyData, Task, unit_no
from app.domain.ranges import PageSpan, normalize, subtract, page_count

CARRY_RATE = 0.5
KEEP_NOTE = "보충 학습은 어느 안에서도 미루지 않아요"
PROPOSAL_TYPES = {"A": "more_time", "B": "reduce_scope", "C": "compress"}
KEY_OF = {v: k for k, v in PROPOSAL_TYPES.items()}
WEEKDAY = "월화수목금토일"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def _mdw(d: date) -> str:
    return f"{d.month}/{d.day} ({WEEKDAY[d.weekday()]})"


def remaining_ranges(task: Task) -> list[dict]:
    """Subtract actual PDF-page unions while retaining the originating range IDs."""
    units = {r["unit_id"]: r.get("material_id", r["unit_id"]) for r in task.paged}
    completed = [PageSpan(units[r["unit_id"]], r["start_page"], r["end_page"])
                 for r in task.done_ranges if r["unit_id"] in units
                 and r["start_page"] is not None and r["end_page"] is not None]
    seen, out = [], []
    for r in task.paged:
        material = units[r["unit_id"]]
        source = PageSpan(material, r["start_page"], r["end_page"])
        for part in subtract([source], [*completed, *seen]):
            out.append({"unit_id": r["unit_id"], "material_id": material,
                        "source_range_id": r.get("id"), "start_page": part.start, "end_page": part.end})
        seen.append(source)
    return out


def _pages(ranges) -> int:
    return page_count(PageSpan(r.get("material_id", r["unit_id"]), r["start_page"], r["end_page"])
                      for r in ranges)


class Planner:
    def __init__(self, conn: Connection, user: dict):
        self.conn = conn
        self.user = user
        self._at = None
        self.data = StudyData(conn, user["id"])
        if not self.data.plan:
            raise ApiError("NO_PLAN", "계획을 먼저 만들어 주세요", 404)

    # ---------- 기준 ----------
    def daily_average(self, subject_id: str) -> float:
        scope = page_count(span for uid, spans in self.data.unit_scopes.items()
                           if self.data.units[uid]["subject_id"] == subject_id for span in spans)
        days = {x.row["scheduled_date"] for x in self.data.tasks  # 보충만 있던 날도 '태스크가 있는 날'
                if x.row["subject_id"] == subject_id and x.row["origin_task_id"] is None}
        return scope / len(days) if days else 0

    def next_study_day(self, subject_id: str, after: date) -> date | None:
        days = sorted({x.row["scheduled_date"] for x in self.data.tasks
                       if x.row["subject_id"] == subject_id and x.row["scheduled_date"] > after
                       and x.row["status"] not in ("carried_over", "excluded")})
        exam = self.data.subject_by_id[subject_id]["exam_date"]
        if days and (exam is None or days[0] < exam):
            return days[0]
        return None

    # ---------- 자정 판정 ----------
    def advance_day(self) -> str:
        """Demo-only date mutation around the shared explicit-date assessment."""
        today = self.user["demo_date"]
        if today is None:
            raise ApiError("NOT_DEMO_USER", "데모 날짜가 없는 사용자예요", 400)
        judgment = self.close_day(today)
        self.conn.execute(update(t.users).where(t.users.c.id == self.user["id"]).values(
            demo_date=today + timedelta(days=1), updated_at=self._at or _now()))
        return judgment

    def close_day(self, day: date, *, allow_active: bool = False, at: datetime | None = None) -> str:
        """Assess one local day without moving a real or demo user's clock.

        D-04 supplies its locked user and closes missed dates chronologically.
        Focus sessions may span midnight, so normal closure preserves active tasks.
        """
        if type(day) is not date or (at is not None and (at.tzinfo is None or at.utcoffset() is None)):
            raise ValueError("A local day and timezone-aware timestamp are required")
        self._at = at
        active = set(self.conn.scalars(select(t.study_sessions.c.task_id).where(
            t.study_sessions.c.user_id == self.user["id"],
            t.study_sessions.c.status.in_(["running", "paused"]))))
        if active and not allow_active:
            raise ApiError("ACTIVE_STUDY_SESSION", "집중 모드를 먼저 끝내 주세요", 409)
        today = day
        nxt = today + timedelta(days=1)
        # Freeze the final daily plan before any carry/exclusion changes its placements.
        from app.services.daily_totals import DailyTotalsService
        DailyTotalsService(self.conn, self.user).snapshot(today, finalize=True, at=self._at or _now())

        # 시험 당일부터는 새 공부 배치를 만들지 않는다. 기록/완료 태스크는 보존한다.
        for subject in self.data.subjects:
            exam = subject["exam_date"]
            if exam is not None and exam <= nxt:
                self.conn.execute(update(t.study_tasks).where(
                    t.study_tasks.c.subject_id == subject["id"],
                    t.study_tasks.c.scheduled_date >= exam,
                    t.study_tasks.c.task_type == "study",
                    t.study_tasks.c.status.in_(["planned", "partial"]),
                    t.study_tasks.c.id.not_in(active)).values(status="excluded"))

        def can_carry(task):
            exam = self.data.subject_by_id[task.row["subject_id"]]["exam_date"]
            return exam is None or nxt < exam

        left = []  # (task, 남은 범위)
        for x in self.data.tasks:
            if x.id not in active and x.row["subject_id"] in self.data.subject_by_id and x.row["scheduled_date"] <= today and x.row["status"] in ("planned", "partial", "missed"):
                if not can_carry(x):
                    exam = self.data.subject_by_id[x.row["subject_id"]]["exam_date"]
                    if today < exam and x.row["status"] == "planned" and not x.record:
                        self.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == x.id).values(status="missed"))
                    continue
                rest = remaining_ranges(x)
                if rest:
                    left.append((x, rest))
        # 아직 고르지 않은 분배안이 있으면 그 밀린 분량도 합쳐서 한 번만 다시 판정해요
        prev = self.pending_recovery()
        if prev:
            by_id = {x.id: x for x in self.data.tasks}
            for item in prev["recovery_context"].get("tasks", []):
                if item["taskId"] not in active and item["taskId"] in by_id and by_id[item["taskId"]].row["subject_id"] in self.data.subject_by_id and can_carry(by_id[item["taskId"]]):
                    task = by_id[item["taskId"]]
                    if task.id not in {candidate.id for candidate, _ in left}:
                        rest = remaining_ranges(task)
                        if rest:
                            left.append((task, rest))
            self.conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == prev["id"])
                              .values(status="stale", updated_at=self._at or _now()))
        overdue_ranges = defaultdict(list)
        for x, rest in left:
            if x.row["task_type"] == "study":
                overdue_ranges[x.row["subject_id"]].extend(rest)
        overdue = {sid: _pages(ranges) for sid, ranges in overdue_ranges.items()}
        over = {sid: pages for sid, pages in overdue.items()
                if pages >= math.ceil(self.daily_average(sid) * CARRY_RATE)}

        carried_items = []
        blocked_ranges = defaultdict(list)
        for x, rest in left:
            if x.row["status"] == "planned" and not x.record and x.row["scheduled_date"] == today:
                self.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == x.id).values(status="missed"))
            if x.row["subject_id"] in over and x.row["task_type"] == "study":
                continue  # 분배안에서 처리
            item = self._carry(x, rest, reason="어제에서 이월", after=today)
            if item is not None:
                carried_items.append(item)
            else:
                blocked_ranges[x.row['subject_id']].extend(rest)
        for sid, ranges in blocked_ranges.items():
            over[sid] = max(over.get(sid, 0), _pages(ranges))

        self._close_quizzes(today)
        self.conn.execute(update(t.users).where(t.users.c.id == self.user["id"]).values(
            last_assessed_date=today, updated_at=self._at or _now()))

        if carried_items:
            self.conn.execute(update(t.plans).where(t.plans.c.id == self.data.plan["id"]).values(
                revision=t.plans.c.revision + 1))
            self._notice(nxt, "carry", f"carry:{nxt.isoformat()}", {"items": carried_items})
        if over:
            self._create_recovery(today, nxt, over, left)
            return "replan"
        return "carry" if carried_items else "none"

    def _carry(self, x: Task, rest: list[dict], reason: str, day: date | None = None,
               minutes: int | None = None, after: date | None = None) -> dict | None:
        from app.services.carryover import append_carry
        sid = x.row["subject_id"]
        day = day or self.next_study_day(sid, after or x.row["scheduled_date"])
        exam = self.data.subject_by_id[sid]["exam_date"]
        if day is None or (exam is not None and day >= exam):
            # Keep the old unfinished obligation visible to future assessments.
            return None
        result = append_carry(self.conn, x, rest, day=day, reason=reason, at=self._at or _now(), minutes=minutes)
        if result is not None:
            avg = self.daily_average(sid)
            result.update(dailyAvg=round(avg), ratio=round(result["pages"] / avg * 100) if avg else 0)
        return result

    def _close_quizzes(self, day: date) -> None:
        from app.services.quiz_finalization import close_daily_quizzes
        close_daily_quizzes(self.conn, self.user, day, at=self._at or _now())

    def _notice(self, day: date, ntype: str, dedupe: str, payload: dict) -> None:
        exists = self.conn.execute(select(t.user_notices.c.id).where(
            t.user_notices.c.user_id == self.user["id"], t.user_notices.c.dedupe_key == dedupe)).first()
        if not exists:
            self.conn.execute(t.user_notices.insert().values(
                id=str(uuid.uuid4()), user_id=self.user["id"], dedupe_key=dedupe, notice_type=ntype,
                notice_date=day, status="pending", payload=payload, created_at=self._at or _now()))

    # ---------- 분배안 ----------
    def _create_recovery(self, day: date, nxt: date, over: dict, left: list) -> None:
        now = self._at or _now()
        targets = [(x, rest) for x, rest in left if x.row["subject_id"] in over and x.row["task_type"] == "study"]
        context = {
            "assessedDate": day.isoformat(),
            "subjects": [{"subjectId": sid, "overduePages": pages, "dailyAveragePages": round(self.daily_average(sid), 1),
                          "thresholdPages": math.ceil(self.daily_average(sid) * CARRY_RATE)} for sid, pages in over.items()],
            "tasks": [{"taskId": x.id, "ranges": rest} for x, rest in targets],
        }
        req_id = str(uuid.uuid4())
        self.conn.execute(t.plan_change_requests.insert().values(
            id=req_id, user_id=self.user["id"], base_plan_id=self.data.plan["id"],
            base_plan_revision=self.data.plan["revision"], recovery_context=context, kind="recovery", status="ready",
            input_payload={"schemaVersion": "1"}, created_at=now, updated_at=now))
    def pending_recovery(self) -> dict | None:
        r = self.conn.execute(select(t.plan_change_requests).where(
            t.plan_change_requests.c.user_id == self.user["id"], t.plan_change_requests.c.kind == "recovery",
            t.plan_change_requests.c.status == "ready").order_by(t.plan_change_requests.c.created_at.desc())).mappings().first()
        return dict(r) if r else None

    def replan_view(self, saved: bool = False) -> dict | None:
        """Read an already computed immutable candidate; never invent options."""
        req = self.pending_recovery()
        if req is None:
            return None
        rows = self.conn.execute(select(t.plan_proposal_states).where(
            t.plan_proposal_states.c.user_id == self.user['id'],
            t.plan_proposal_states.c.kind == 'recovery_options',
            t.plan_proposal_states.c.status == 'ready')).mappings()
        for row in rows:
            if row['target'] == {'recoveryRequestId': req['id']}:
                return {**row['preview_response'], 'saved': saved}
        return None
