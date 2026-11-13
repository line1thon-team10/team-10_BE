"""집중 모드 (HM-07~10, 정의서 4-10).

- [시작]부터 [종료]까지 멈춘 시간을 빼고 서버 시계로 자동 측정 → 새로고침해도 이어져요
- 시작·재개마다 '순공부 구간(segment)'을 열고, 멈춤·종료 때 닫아요
- 예상 시간의 2배가 지나면 "아직 공부 중이에요?"(checkIn). 10분 동안 답이 없으면
  알림이 뜬 시점에서 자동 종료하고, 그 기록은 속도 보정에서 빼요
- 화면 상태 이름: active | paused | ended (DB는 running | paused | ended | auto_stopped)
"""
import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Connection, func, select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.services.study import StudyData

CHECK_WAIT = timedelta(minutes=10)
FRONT_STATUS = {"running": "active", "paused": "paused", "ended": "ended", "auto_stopped": "ended"}
QUOTES = [  # 고정 문구 10개 (HM-10, 프론트 seed.js focusQuotes와 같음)
    "벼락치기 말고, 피뢰침.",
    "지금은 이 범위만 보면 돼요.",
    "오늘 한 페이지가 시험 날의 한 문제예요.",
    "다 못 해도 괜찮아요. 한 만큼 쌓여요.",
    "밀려도 무너지지 않아요. 다시 맞추면 돼요.",
    "천천히 읽은 페이지가 오래 남아요.",
    "오늘의 블록 하나, 지금 쌓는 중이에요.",
    "어려운 곳에서 멈춘 것도 공부예요.",
    "한 번 더 본 개념은 덜 흔들려요.",
    "시험은 한 번, 준비는 매일 조금씩.",
]


def now() -> datetime:  # 테스트에서 바꿔 끼울 수 있게 함수로 둬요
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class FocusService:
    def __init__(self, conn: Connection, user: dict, today: date):
        self.conn = conn
        self.user = user
        self.today = today

    # ---------- 읽기 ----------
    def _session(self, session_id: str) -> dict:
        s = self.conn.execute(select(t.study_sessions).where(
            t.study_sessions.c.id == session_id, t.study_sessions.c.user_id == self.user["id"])).mappings().first()
        if not s:
            raise ApiError("SESSION_NOT_FOUND", "집중 기록을 찾지 못했어요", 404)
        return dict(s)

    def _segments(self, session_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(select(t.study_session_segments).where(
            t.study_session_segments.c.session_id == session_id).order_by(t.study_session_segments.c.sort_order)).mappings()]

    def _elapsed(self, segs: list[dict], at: datetime) -> float:
        """at 시점까지 순공부 초"""
        total = 0.0
        for g in segs:
            start, end = _aware(g["started_at"]), _aware(g["ended_at"]) or at
            if start < at:
                total += (min(end, at) - start).total_seconds()
        return total

    def _expected_seconds(self, s: dict) -> int:
        minutes = self.conn.execute(select(t.study_tasks.c.estimated_minutes).where(
            t.study_tasks.c.id == s["task_id"])).scalar() or 30
        return minutes * 60

    # ---------- 2배 확인·자동 종료 (HM-09) ----------
    def _check(self, s: dict) -> dict:
        if s["status"] != "running":
            return s
        segs = self._segments(s["id"])
        current = now()
        limit = s["last_confirmed_elapsed_seconds"] + 2 * self._expected_seconds(s)
        prompted = _aware(s["check_prompted_at"])
        if prompted is None:
            if self._elapsed(segs, current) < limit:
                return s
            # 순공부 시간이 기준에 닿은 실제 시각 = 열린 구간 시작 + (기준 − 닫힌 구간 합)
            closed = sum((_aware(g["ended_at"]) - _aware(g["started_at"])).total_seconds() for g in segs if g["ended_at"])
            opened = next(g for g in segs if g["ended_at"] is None)
            prompted = _aware(opened["started_at"]) + timedelta(seconds=limit - closed)
            self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == s["id"]).values(
                check_prompted_at=prompted))
        if current >= prompted + CHECK_WAIT:
            self._close(s["id"], prompted, status="auto_stopped", excluded=True)
        return self._session(s["id"])

    def _close(self, session_id: str, at: datetime, status: str, excluded: bool = False) -> None:
        self.conn.execute(update(t.study_session_segments).where(
            t.study_session_segments.c.session_id == session_id, t.study_session_segments.c.ended_at.is_(None))
            .values(ended_at=at))
        measured = round(self._elapsed(self._segments(session_id), at))
        values = dict(status=status, ended_at=at, measured_seconds=measured, check_prompted_at=None, paused_at=None)
        if excluded:
            values["excluded_from_calibration"] = True
        self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(**values))

    # ---------- 화면 모양 ----------
    def view(self, s: dict) -> dict:
        s = self._check(s)
        data = StudyData(self.conn, self.user["id"])
        task = next((x for x in data.tasks if x.id == s["task_id"]), None)
        subject = data.subject_by_id.get(task.row["subject_id"]) if task else None
        segs = self._segments(s["id"])
        current = now()
        if s["status"] in ("ended", "auto_stopped"):
            active = s["measured_seconds"] or 0
            end_at = _aware(s["ended_at"])
        else:
            active = self._elapsed(segs, current)
            end_at = current
        wall = (end_at - _aware(s["started_at"])).total_seconds()
        material_id = None
        if task and task.ranges:
            material_id = data.units[task.ranges[0]["unit_id"]]["material_id"]
        recorded = self.conn.execute(select(t.study_records.c.id).where(
            t.study_records.c.session_id == s["id"])).first() is not None
        return {
            "id": s["id"], "taskId": s["task_id"],
            "subjectId": subject["id"] if subject else None, "subjectName": subject["name"] if subject else None,
            "task": {"title": task.row["title"], "from": task.first, "to": task.last, "pages": task.pages,
                     "minutes": task.row["estimated_minutes"]} if task else None,
            "status": FRONT_STATUS[s["status"]],
            "quote": QUOTES[int(hashlib.md5(s["id"].encode()).hexdigest(), 16) % len(QUOTES)],
            "material": {"id": material_id, "page": task.first if task else 1},
            "minutes": int(active // 60), "pausedMinutes": int(max(0, wall - active) // 60),
            "checkIn": s["status"] == "running" and s["check_prompted_at"] is not None,
            "autoStopped": s["status"] == "auto_stopped",
            "excludeFromPace": bool(s["excluded_from_calibration"]),
            "recorded": recorded,
            "todayMinutes": self._sum_minutes(t.study_sessions.c.study_date == self.today, s, active),
            "subjectMinutes": self._sum_minutes(
                t.study_sessions.c.task_id.in_(select(t.study_tasks.c.id).where(
                    t.study_tasks.c.subject_id == (subject["id"] if subject else None))), s, active),
        }

    def _sum_minutes(self, cond, s: dict, active_now: float) -> int:
        """오늘 총·과목 누적 = 끝난 측정 세션의 합 (+ 지금 재고 있는 세션)"""
        done = self.conn.execute(select(func.coalesce(func.sum(t.study_sessions.c.measured_seconds), 0)).where(
            t.study_sessions.c.user_id == self.user["id"], cond,
            t.study_sessions.c.status.in_(["ended", "auto_stopped"]))).scalar()
        extra = active_now if s["status"] in ("running", "paused") else 0
        return int((done + extra) // 60)

    # ---------- 동작 ----------
    def active(self) -> dict | None:
        s = self.conn.execute(select(t.study_sessions).where(
            t.study_sessions.c.user_id == self.user["id"],
            t.study_sessions.c.status.in_(["running", "paused"]))).mappings().first()
        if not s:
            return None
        v = self.view(dict(s))
        return v if v["status"] != "ended" else None

    def start(self, task_id: str) -> dict:
        data = StudyData(self.conn, self.user["id"])
        task = next((x for x in data.tasks if x.id == task_id), None)
        if not task:
            raise ApiError("TASK_NOT_FOUND", "태스크를 찾지 못했어요", 404)
        if task.row["status"] not in ("planned", "partial"):
            raise ApiError("TASK_DONE", "이미 끝낸 태스크예요")
        running = self.conn.execute(select(t.study_sessions).where(
            t.study_sessions.c.user_id == self.user["id"],
            t.study_sessions.c.status.in_(["running", "paused"]))).mappings().first()
        if running and running["task_id"] == task_id:
            return self.view(dict(running))  # 새로고침·다시 들어옴 → 이어서
        if running:
            raise ApiError("OTHER_ACTIVE", "다른 태스크 공부가 아직 진행 중이에요", 409)
        sid, at = str(uuid.uuid4()), now()
        self.conn.execute(t.study_sessions.insert().values(
            id=sid, user_id=self.user["id"], task_id=task_id, status="running", started_at=at,
            last_confirmed_elapsed_seconds=0, study_date=self.today, created_at=at))
        self.conn.execute(t.study_session_segments.insert().values(
            id=str(uuid.uuid4()), session_id=sid, sort_order=1, started_at=at))
        return self.view(self._session(sid))

    def pause(self, session_id: str) -> dict:
        s = self._check(self._session(session_id))
        if s["status"] != "running":
            raise ApiError("NOT_ACTIVE", "공부 중일 때만 멈출 수 있어요")
        at = now()
        self.conn.execute(update(t.study_session_segments).where(
            t.study_session_segments.c.session_id == session_id, t.study_session_segments.c.ended_at.is_(None))
            .values(ended_at=at))
        values = dict(status="paused", paused_at=at)
        if s["check_prompted_at"] is not None:  # 확인 대기 중 멈춤 = 아직 공부 중이라는 뜻
            values.update(check_prompted_at=None,
                          last_confirmed_elapsed_seconds=round(self._elapsed(self._segments(session_id), at)))
        self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(**values))
        return self.view(self._session(session_id))

    def resume(self, session_id: str) -> dict:
        """멈춘 상태면 다시 시작, 공부 중이면 '아직 공부 중이에요?'에 [계속] 응답"""
        s = self._check(self._session(session_id))
        at = now()
        if s["status"] == "paused":
            order = len(self._segments(session_id)) + 1
            self.conn.execute(t.study_session_segments.insert().values(
                id=str(uuid.uuid4()), session_id=session_id, sort_order=order, started_at=at))
            self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(
                status="running", paused_at=None))
        elif s["status"] == "running":
            self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(
                check_prompted_at=None,
                last_confirmed_elapsed_seconds=round(self._elapsed(self._segments(session_id), at))))
        else:
            raise ApiError("ENDED", "이미 끝난 공부예요")
        return self.view(self._session(session_id))

    def end(self, session_id: str) -> dict:
        s = self._check(self._session(session_id))
        if s["status"] in ("running", "paused"):
            self._close(session_id, now(), status="ended")
        return self.view(self._session(session_id))
