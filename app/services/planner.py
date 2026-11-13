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
    """계획 범위에서 기록한 범위를 뺀 남은 범위"""
    done = defaultdict(set)
    for r in task.done_ranges:
        if r["start_page"] is not None:
            done[r["unit_id"]].update(range(r["start_page"], r["end_page"] + 1))
    out = []
    for r in task.paged:
        pages = [p for p in range(r["start_page"], r["end_page"] + 1) if p not in done[r["unit_id"]]]
        if not pages:
            continue
        start = prev = pages[0]
        for p in pages[1:] + [None]:
            if p is not None and p == prev + 1:
                prev = p
                continue
            out.append({"unit_id": r["unit_id"], "start_page": start, "end_page": prev})
            if p is not None:
                start = prev = p
    return out


def _pages(ranges) -> int:
    return sum(r["end_page"] - r["start_page"] + 1 for r in ranges)


class Planner:
    def __init__(self, conn: Connection, user: dict):
        self.conn = conn
        self.user = user
        self.data = StudyData(conn, user["id"])
        if not self.data.plan:
            raise ApiError("NO_PLAN", "계획을 먼저 만들어 주세요", 404)

    # ---------- 기준 ----------
    def daily_average(self, subject_id: str) -> float:
        scope = sum(u["end_page"] - u["start_page"] + 1 for u in self.data.units.values()
                    if u["subject_id"] == subject_id and u["in_scope"] and u["start_page"] is not None)
        days = {x.row["scheduled_date"] for x in self.data.tasks  # 보충만 있던 날도 '태스크가 있는 날'
                if x.row["subject_id"] == subject_id and x.row["origin_task_id"] is None}
        return scope / len(days) if days else 0

    def next_study_day(self, subject_id: str, after: date) -> date:
        days = sorted({x.row["scheduled_date"] for x in self.data.tasks
                       if x.row["subject_id"] == subject_id and x.row["scheduled_date"] > after
                       and x.row["status"] not in ("carried_over", "excluded")})
        exam = self.data.subject_by_id[subject_id]["exam_date"]
        if days and (exam is None or days[0] < exam):
            return days[0]
        return after + timedelta(days=1)

    # ---------- 자정 판정 ----------
    def advance_day(self) -> str:
        """오늘을 마감하고 다음 날로. 결과: carry(이월) | replan(분배안) | none"""
        running = self.conn.execute(select(t.study_sessions.c.id).where(
            t.study_sessions.c.user_id == self.user["id"],
            t.study_sessions.c.status.in_(["running", "paused"]))).first()
        if running:
            raise ApiError("ACTIVE_STUDY_SESSION", "집중 모드를 먼저 끝내 주세요", 409)
        today = self.user["demo_date"]
        if today is None:
            raise ApiError("NOT_DEMO_USER", "데모 날짜가 없는 사용자예요", 400)
        nxt = today + timedelta(days=1)

        # 시험 당일부터는 새 공부 배치를 만들지 않는다. 기록/완료 태스크는 보존한다.
        for subject in self.data.subjects:
            exam = subject["exam_date"]
            if exam is not None and exam <= nxt:
                self.conn.execute(update(t.study_tasks).where(
                    t.study_tasks.c.subject_id == subject["id"],
                    t.study_tasks.c.scheduled_date >= exam,
                    t.study_tasks.c.status.in_(["planned", "partial"])).values(status="excluded"))

        def can_carry(task):
            exam = self.data.subject_by_id[task.row["subject_id"]]["exam_date"]
            return exam is None or nxt < exam

        left = []  # (task, 남은 범위)
        for x in self.data.tasks_on(today):
            if x.row["status"] in ("planned", "partial"):
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
                if item["taskId"] in by_id and can_carry(by_id[item["taskId"]]):
                    left.append((by_id[item["taskId"]], item["ranges"]))
            self.conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == prev["id"])
                              .values(status="stale", updated_at=_now()))
        overdue = defaultdict(int)
        for x, rest in left:
            if x.row["task_type"] == "study":
                overdue[x.row["subject_id"]] += _pages(rest)
        over = {sid: pages for sid, pages in overdue.items()
                if pages >= math.ceil(self.daily_average(sid) * CARRY_RATE)}

        carried_items = []
        for x, rest in left:
            if x.row["status"] == "planned" and not x.record and x.row["scheduled_date"] == today:
                self.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == x.id).values(status="missed"))
            if x.row["subject_id"] in over and x.row["task_type"] == "study":
                continue  # 분배안에서 처리
            carried_items.append(self._carry(x, rest, reason="어제에서 이월"))

        self._close_quizzes(today)
        self.conn.execute(update(t.daily_plan_totals).where(
            t.daily_plan_totals.c.user_id == self.user["id"], t.daily_plan_totals.c.study_date == today)
            .values(finalized_at=_now()))
        self.conn.execute(update(t.users).where(t.users.c.id == self.user["id"]).values(
            demo_date=nxt, last_assessed_date=nxt, updated_at=_now()))

        if carried_items:
            self._notice(nxt, "carry", f"carry:{nxt.isoformat()}", {"items": carried_items})
        if over:
            self._create_recovery(today, nxt, over, left)
            return "replan"
        return "carry" if carried_items else "none"

    def _carry(self, x: Task, rest: list[dict], reason: str, day: date | None = None,
               minutes: int | None = None) -> dict:
        sid = x.row["subject_id"]
        day = day or self.next_study_day(sid, x.row["scheduled_date"])
        pages = _pages(rest)
        mpp = float(self.data.subject_by_id[sid]["minutes_per_page"] or 2.5)
        minutes = minutes or max(15, round(pages * mpp / 5) * 5)
        task_id = str(uuid.uuid4())
        self.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == x.id).values(status="carried_over"))
        self.conn.execute(t.study_tasks.insert().values(
            id=task_id, plan_id=x.row["plan_id"], subject_id=sid, block_id=x.row["block_id"], origin_task_id=x.id,
            task_type=x.row["task_type"], title=x.row["title"], scheduled_date=day, estimated_minutes=minutes,
            placement_reason=reason, status="planned", created_at=_now()))
        for i, r in enumerate(rest):
            self.conn.execute(t.study_task_ranges.insert().values(
                id=str(uuid.uuid4()), task_id=task_id, unit_id=r["unit_id"], start_page=r["start_page"],
                end_page=r["end_page"], sort_order=i + 1))
        avg = self.daily_average(sid)
        return {"subjectId": sid, "from": rest[0]["start_page"], "to": rest[-1]["end_page"], "pages": pages,
                "dailyAvg": round(avg), "ratio": round(pages / avg * 100) if avg else 0, "date": day.isoformat()}

    def _close_quizzes(self, day: date) -> None:
        """안 푼 오늘의 퀴즈는 닫고(carried_over) 다음 퀴즈에 합쳐져요 (4-4)"""
        self.conn.execute(update(t.quiz_sessions).where(
            t.quiz_sessions.c.user_id == self.user["id"], t.quiz_sessions.c.quiz_date == day,
            t.quiz_sessions.c.status.in_(["ready", "in_progress"])).values(status="carried_over", finalized_at=_now()))

    def _notice(self, day: date, ntype: str, dedupe: str, payload: dict) -> None:
        exists = self.conn.execute(select(t.user_notices.c.id).where(
            t.user_notices.c.user_id == self.user["id"], t.user_notices.c.dedupe_key == dedupe)).first()
        if not exists:
            self.conn.execute(t.user_notices.insert().values(
                id=str(uuid.uuid4()), user_id=self.user["id"], dedupe_key=dedupe, notice_type=ntype,
                notice_date=day, status="pending", payload=payload, created_at=_now()))

    # ---------- 분배안 ----------
    def _create_recovery(self, day: date, nxt: date, over: dict, left: list) -> None:
        now = _now()
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
        for key, option in self._options(nxt, targets).items():
            self.conn.execute(t.plan_proposals.insert().values(
                id=str(uuid.uuid4()), request_id=req_id, proposal_type=PROPOSAL_TYPES[key],
                description=option["preview"], changes=option, created_at=now))

    def _options(self, nxt: date, targets: list) -> dict:
        total_pages = sum(_pages(rest) for _, rest in targets)
        base_minutes = {}
        for x, rest in targets:
            mpp = float(self.data.subject_by_id[x.row["subject_id"]]["minutes_per_page"] or 2.5)
            base_minutes[x.id] = max(15, round(_pages(rest) * mpp / 5) * 5)
        total_min = sum(base_minutes.values())

        # A: 다음 이틀에 나눠 시간 늘리기
        days = [nxt, nxt + timedelta(days=1)]
        per_day = math.ceil(total_min / len(days) / 5) * 5
        first_x, first_rest = targets[0]
        a = {"key": "A", "lines": [f"{'·'.join(_md(d) for d in days)} 하루 {per_day}분씩 추가", "범위는 그대로"],
             "effect": "범위 100% 유지",
             "preview": f"{_mdw(nxt)} {self._label(first_x, first_rest)} 추가 · 적용하면 회복 박스 팝업",
             "perDayMinutes": per_day, "days": [d.isoformat() for d in days]}

        # B: 중요도 낮은 범위 빼기 — 밀린 과목마다 아직 안 한 마지막 개념의 끝부분(최대 6p)
        exclusions = self._exclusions({x.row["subject_id"] for x, _ in targets})
        ex_pages = sum(e["end_page"] - e["start_page"] + 1 for e in exclusions)
        b = {"key": "B",
             "lines": [f"{e['subjectName']} {e['unitNo']}단원 {e['title']}(p.{e['start_page']}–{e['end_page']}, "
                       f"{e['end_page'] - e['start_page'] + 1}p) 제외" for e in exclusions] or ["뺄 수 있는 범위가 없어요"],
             "effect": f"시간 유지 · {ex_pages}p 제외",
             "preview": " · ".join(f"{e['subjectName']} {e['unitNo']}단원 {e['title']}({e['end_page'] - e['start_page'] + 1}p)"
                                  for e in exclusions) + "를 계획에서 빼요 · 적용하면 회복 박스 팝업",
             "exclusions": exclusions}

        # C: 같은 범위를 핵심 위주로 30% 단축
        c_min = max(15, round(base_minutes[first_x.id] * 0.7))
        c = {"key": "C", "lines": ["밀린 태스크를 핵심 요약 위주로", "태스크 시간 30% 단축"], "effect": "1회독 깊이 얕아짐",
             "preview": f"{_mdw(nxt)} {self._label(first_x, first_rest)}를 {c_min}분으로 줄여 넣어요 · 적용하면 회복 박스 팝업"}
        for opt in (a, b, c):
            opt["totalPages"] = total_pages
        return {"A": a, "B": b, "C": c}

    def _label(self, x: Task, rest: list[dict]) -> str:
        s = self.data.subject_by_id[x.row["subject_id"]]
        u = self.data.units.get(rest[0]["unit_id"])
        return f"{s['name']} · {unit_no(u['code'])}단원 p.{rest[0]['start_page']}–{rest[-1]['end_page']}"

    def _exclusions(self, subject_ids: set[str]) -> list[dict]:
        studied = defaultdict(set)
        for x in self.data.tasks:
            for r in x.done_ranges:
                if r["start_page"] is not None:
                    studied[r["unit_id"]].update(range(r["start_page"], r["end_page"] + 1))
        out = []
        for sid in subject_ids:
            units = sorted([u for u in self.data.units.values() if u["subject_id"] == sid and u["in_scope"]
                            and u["start_page"] is not None and float(u["emphasis_weight"] or 1) <= 1.0],
                           key=lambda u: u["sort_order"])
            for u in reversed(units):
                concepts = self.conn.execute(select(t.concepts).where(
                    t.concepts.c.unit_id == u["id"], t.concepts.c.in_scope.is_(True))
                    .order_by(t.concepts.c.sort_order.desc())).mappings().all()
                c = next((c for c in concepts if not (set(range(c["start_page"], c["end_page"] + 1)) & studied[u["id"]])), None)
                if c:
                    start = max(c["start_page"], c["end_page"] - 5)
                    out.append({"subjectId": sid, "subjectName": self.data.subject_by_id[sid]["name"],
                                "unitId": u["id"], "unitNo": unit_no(u["code"]), "conceptId": c["id"],
                                "title": c["title"], "start_page": start, "end_page": c["end_page"]})
                    break
        return out

    def pending_recovery(self) -> dict | None:
        r = self.conn.execute(select(t.plan_change_requests).where(
            t.plan_change_requests.c.user_id == self.user["id"], t.plan_change_requests.c.kind == "recovery",
            t.plan_change_requests.c.status == "ready").order_by(t.plan_change_requests.c.created_at.desc())).mappings().first()
        return dict(r) if r else None

    def replan_view(self, saved: bool = False) -> dict | None:
        req = self.pending_recovery()
        if not req:
            return None
        props = self.conn.execute(select(t.plan_proposals).where(t.plan_proposals.c.request_id == req["id"])).mappings().all()
        options = sorted([{k: p["changes"][k] for k in ("key", "lines", "effect", "preview")} for p in props],
                         key=lambda o: o["key"])
        ctx = req["recovery_context"]
        overdue = [{"subjectId": s["subjectId"], "pages": s["overduePages"], "dailyAvg": round(s["dailyAveragePages"])}
                   for s in ctx["subjects"]]
        return {"kind": "overdue", "saved": saved, "title": None, "date": self.user["demo_date"].isoformat()
                if self.user["demo_date"] else None, "totalPages": sum(o["pages"] for o in overdue),
                "overdue": overdue, "keepNote": KEEP_NOTE, "options": options}

    def apply(self, key: str) -> dict:
        req = self.pending_recovery()
        if not req:
            raise ApiError("NO_REPLAN", "적용할 분배안이 없어요", 404)
        prop = self.conn.execute(select(t.plan_proposals).where(
            t.plan_proposals.c.request_id == req["id"],
            t.plan_proposals.c.proposal_type == PROPOSAL_TYPES.get(key, "-"))).mappings().first()
        if not prop:
            raise ApiError("BAD_KEY", "분배안을 다시 골라 주세요")
        changes = prop["changes"]
        tasks = {x.id: x for x in self.data.tasks}
        nxt = self.user["demo_date"]
        for item in req["recovery_context"]["tasks"]:
            x = tasks.get(item["taskId"])
            if not x:
                continue
            rest = item["ranges"]
            if key == "A":
                self._carry(x, rest, reason=f"분배안 A · 공부 시간 {changes['perDayMinutes']}분 추가", day=nxt)
            elif key == "B":
                self._carry(x, rest, reason="분배안 B · 중요도 낮은 범위를 뺀 자리에 넣었어요", day=nxt)
            else:
                mpp = float(self.data.subject_by_id[x.row["subject_id"]]["minutes_per_page"] or 2.5)
                minutes = max(15, round(_pages(rest) * mpp * 0.7))
                self._carry(x, rest, reason="분배안 C · 핵심 요약 위주로 30% 단축", day=nxt, minutes=minutes)
        if key == "B":
            for e in changes.get("exclusions", []):
                self._exclude(e)
        now = _now()
        self.conn.execute(update(t.plan_change_requests).where(t.plan_change_requests.c.id == req["id"]).values(
            status="accepted", accepted_proposal_id=prop["id"], decided_at=now, updated_at=now))
        self.conn.execute(update(t.plans).where(t.plans.c.id == self.data.plan["id"]).values(
            revision=self.data.plan["revision"] + 1))
        self._notice(nxt, "recovery", f"replanned:{req['id']}", {"key": key})
        return {"type": "box", "reason": "recover", "planKey": key}

    def _exclude(self, e: dict) -> None:
        """앞으로 할 태스크 범위에서 뺀 페이지를 잘라내요. 범위가 다 빠지면 태스크는 excluded"""
        rows = self.conn.execute(
            select(t.study_task_ranges, t.study_tasks.c.status, t.study_tasks.c.task_type)
            .join(t.study_tasks, t.study_task_ranges.c.task_id == t.study_tasks.c.id)
            .where(t.study_task_ranges.c.unit_id == e["unitId"], t.study_tasks.c.status == "planned",
                   t.study_tasks.c.task_type == "study")).mappings().all()
        for r in rows:
            if r["start_page"] is None or r["end_page"] < e["start_page"] or r["start_page"] > e["end_page"]:
                continue
            if r["start_page"] >= e["start_page"] and r["end_page"] <= e["end_page"]:
                self.conn.execute(t.study_task_ranges.delete().where(t.study_task_ranges.c.id == r["id"]))
            elif r["start_page"] < e["start_page"]:
                self.conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.id == r["id"])
                                  .values(end_page=e["start_page"] - 1))
            else:
                self.conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.id == r["id"])
                                  .values(start_page=e["end_page"] + 1))
            left = self.conn.execute(select(t.study_task_ranges.c.id).where(
                t.study_task_ranges.c.task_id == r["task_id"])).first()
            if not left:
                self.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == r["task_id"]).values(status="excluded"))
