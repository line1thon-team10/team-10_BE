"""집중 모드 (HM-07~10, 정의서 4-10).

- [시작]부터 [종료]까지 멈춘 시간을 빼고 서버 시계로 자동 측정 → 새로고침해도 이어져요
- 시작·재개마다 '순공부 구간(segment)'을 열고, 멈춤·종료 때 닫아요
- 예상 시간의 2배가 지나면 "아직 공부 중이에요?"(checkIn). 10분 동안 답이 없으면
  알림이 뜬 시점에서 자동 종료하고, 그 기록은 속도 보정에서 빼요
- 화면 상태 이름: active | paused | ended (DB는 running | paused | ended | auto_stopped)
"""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.focus import FocusRepository
from app.domain.focus import quote_for
from app.repositories.locking import UserBusyError, lock_user

CHECK_WAIT = timedelta(minutes=10)
FRONT_STATUS = {"running": "active", "paused": "paused", "ended": "ended", "auto_stopped": "ended"}



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
        self.at = None

    def _prepare(self):
        if self.at is not None:
            return
        try:
            fresh = lock_user(self.conn, uuid.UUID(self.user["id"]))
        except UserBusyError as exc:
            raise ApiError("REQUEST_IN_PROGRESS", "다른 요청이 처리 중이에요. 잠시 후 다시 시도해 주세요", 409) from exc
        if fresh is None:
            raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
        self.user = fresh.to_mapping()
        self.at = now()
        self.today = self.user.get("demo_date") or self.at.astimezone(ZoneInfo(self.user["timezone"])).date()

    def _active_row(self):
        row = self.conn.execute(select(t.study_sessions).where(
            t.study_sessions.c.user_id == self.user["id"],
            t.study_sessions.c.status.in_(["running", "paused"]))).mappings().first()
        return self._check(dict(row)) if row else None

    def _snapshot(self, s):
        values = {}
        if s.get("expected_seconds") is None:
            minutes = self.conn.scalar(select(t.study_tasks.c.estimated_minutes).where(t.study_tasks.c.id == s["task_id"]))
            values["expected_seconds"] = max(1, int(minutes or 30)) * 60
        if s.get("quote") is None:
            values["quote"] = quote_for(s["id"])
        if values:
            self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == s["id"]).values(**values))
            s = {**s, **values}
        return s

    # ---------- 읽기 ----------
    def _session(self, session_id: str) -> dict:
        try:
            session_id = str(uuid.UUID(str(session_id)))
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "올바른 세션 UUID가 필요해요", 422) from exc
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
                total += max(0, (min(end, at) - start).total_seconds())
        return total

    # ---------- 2배 확인·자동 종료 ----------
    def _check(self, s: dict) -> dict:
        s = self._snapshot(s)
        segs = self._segments(s["id"])
        # 서버 시계가 되돌아가도 역전 구간을 기록하지 않는다.
        boundaries = [_aware(s["started_at"])] + [_aware(g["ended_at"] or g["started_at"]) for g in segs]
        self.at = max(self.at, *boundaries)
        if s["status"] != "running":
            return s
        if sum(g["ended_at"] is None for g in segs) != 1:
            raise ApiError("SERVER_ERROR", "집중 측정 구간을 확인하지 못했어요", 500)
        limit = s["last_confirmed_elapsed_seconds"] + 2 * s["expected_seconds"]
        prompted = _aware(s["check_prompted_at"])
        if prompted is None:
            if self._elapsed(segs, self.at) < limit:
                return s
            remaining = limit
            for g in segs:
                start = _aware(g["started_at"])
                end = _aware(g["ended_at"]) or self.at
                duration = max(0, (end - start).total_seconds())
                if remaining <= duration:
                    prompted = start + timedelta(seconds=remaining)
                    break
                remaining -= duration
            self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == s["id"])
                              .values(check_prompted_at=prompted))
        if self.at >= prompted + CHECK_WAIT:
            self._close(s["id"], prompted, status="auto_stopped", excluded=True)
        return self._session(s["id"])

    def _close(self, session_id: str, at: datetime, status: str, excluded: bool = False) -> None:
        self.conn.execute(update(t.study_session_segments).where(
            t.study_session_segments.c.session_id == session_id, t.study_session_segments.c.ended_at.is_(None))
            .values(ended_at=at))
        measured = int(self._elapsed(self._segments(session_id), at))
        values = dict(status=status, ended_at=at, measured_seconds=measured, check_prompted_at=None, paused_at=None)
        if excluded:
            values["excluded_from_calibration"] = True
        self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(**values))

    # ---------- 화면 모양 ----------
    def view(self, s: dict) -> dict:
        self._prepare()
        self._active_row()  # 다른 과거 세션 조회에서도 오늘 합계의 자동 종료를 먼저 정산한다.
        s = self._check(self._session(s["id"]))
        repo = FocusRepository(self.conn, self.user["id"])
        task = repo.task(s["task_id"])
        if task is None:
            raise ApiError("SESSION_NOT_FOUND", "집중 기록을 찾지 못했어요", 404)
        raw_ranges = repo.ranges(task)
        records = repo.record_ranges(task["id"])
        ranges, pages_by_material = [], {}
        for r in raw_ranges:
            if r["start_page"] is None or r["end_page"] is None:
                continue
            done = [min(rec["end_page"], r["end_page"]) for rec in records
                    if rec["unit_id"] == r["unit_id"] and rec["end_page"] is not None
                    and rec["start_page"] <= r["end_page"] and rec["end_page"] >= r["start_page"]]
            ranges.append({"id":r["id"], "materialId":r["material_id"], "unitId":r["unit_id"],
                           "conceptId":r["concept_id"], "from":r["start_page"], "to":r["end_page"],
                           "doneTo":max(done) if done else None})
            pages_by_material.setdefault(r["material_id"],set()).update(range(r["start_page"],r["end_page"]+1))
        first = ranges[0] if len(ranges)==1 else None
        material = {"id":ranges[0]["materialId"], "page":ranges[0]["from"]} if ranges else None
        segs = self._segments(s["id"])
        ended = s["status"] in ("ended", "auto_stopped")
        active = s["measured_seconds"] or 0 if ended else self._elapsed(segs,self.at)
        end_at = _aware(s["ended_at"]) if ended else self.at
        wall = max(0,(end_at - _aware(s["started_at"])).total_seconds())
        recorded = self.conn.execute(select(t.study_records.c.id).where(t.study_records.c.session_id==s["id"])).first() is not None
        today_minutes, subject_minutes = self._totals(repo, task["subject_id"])
        return {
            "id":s["id"], "taskId":s["task_id"], "subjectId":task["subject_id"], "subjectName":task["subject_name"],
            "task":{"title":task["title"], "from":first["from"] if first else None, "to":first["to"] if first else None,
                    "pages":sum(len(pages) for pages in pages_by_material.values()), "ranges":ranges,
                    "minutes":s["expected_seconds"] / 60},
            "status":FRONT_STATUS[s["status"]], "quote":s["quote"], "material":material,
            "minutes":int(active // 60), "pausedMinutes":int(max(0,wall-active)//60),
            "checkIn":s["status"]=="running" and s["check_prompted_at"] is not None,
            "autoStopped":s["status"]=="auto_stopped", "excludeFromPace":bool(s["excluded_from_calibration"]),
            "recorded":recorded, "todayMinutes":today_minutes, "subjectMinutes":subject_minutes,
        }

    def _totals(self, repo, subject_id):
        zone = ZoneInfo(self.user.get("timezone") or "Asia/Seoul")
        start_day = datetime.combine(self.today,time.min,zone).astimezone(timezone.utc)
        end_day = datetime.combine(self.today+timedelta(days=1),time.min,zone).astimezone(timezone.utc)
        today_seconds, subject_seconds = 0.0, 0.0
        for segment in repo.all_segments():
            start = _aware(segment["started_at"])
            end = min(_aware(segment["ended_at"]) or self.at,self.at)
            seconds = max(0,(end-start).total_seconds())
            if segment["subject_id"]==subject_id:
                subject_seconds += seconds
            if self.user.get("demo_date") is not None:
                if segment["study_date"] == self.today:
                    today_seconds += seconds
            else:
                today_seconds += max(0,(min(end,end_day)-max(start,start_day)).total_seconds())
        return int(today_seconds//60), int(subject_seconds//60)

    # ---------- 동작 ----------
    def get(self, session_id):
        self._prepare()
        return self.view(self._session(session_id))

    def active(self) -> dict | None:
        self._prepare()
        s = self._active_row()
        return self.view(s) if s and s["status"] in ("running", "paused") else None

    def start(self, task_id: str) -> dict:
        self._prepare()
        try:
            task_id = str(uuid.UUID(str(task_id)))
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "올바른 태스크 UUID가 필요해요", 422) from exc
        running = self._active_row()
        if running and running["status"] in ("running", "paused") and running["task_id"] == task_id:
            return self.view(running)
        task = self.conn.execute(select(t.study_tasks).join(t.subjects, t.study_tasks.c.subject_id == t.subjects.c.id)
            .join(t.plans, t.study_tasks.c.plan_id == t.plans.c.id).where(
                t.study_tasks.c.id == task_id, t.subjects.c.user_id == self.user["id"],
                t.plans.c.user_id == self.user["id"], t.plans.c.approved_at.is_not(None),
                t.subjects.c.status == "active", t.study_tasks.c.scheduled_date == self.today)).mappings().first()
        if not task:
            raise ApiError("TASK_NOT_FOUND", "오늘의 활성 태스크를 찾지 못했어요", 404)
        if task["status"] not in ("planned", "partial"):
            raise ApiError("TASK_DONE", "이미 끝낸 태스크예요", 409)
        if running and running["status"] in ("running", "paused"):
            raise ApiError("OTHER_ACTIVE", "다른 태스크 공부가 아직 진행 중이에요", 409)
        sid, at = str(uuid.uuid4()), self.at
        self.conn.execute(t.study_sessions.insert().values(
            id=sid, user_id=self.user["id"], task_id=task_id, status="running", started_at=at,
            expected_seconds=max(1, int(task["estimated_minutes"] or 30)) * 60, quote=quote_for(sid),
            last_confirmed_elapsed_seconds=0, study_date=self.today, created_at=at))
        self.conn.execute(t.study_session_segments.insert().values(
            id=str(uuid.uuid4()), session_id=sid, sort_order=1, started_at=at))
        return self.view(self._session(sid))

    def pause(self, session_id: str) -> dict:
        self._prepare()
        s = self._check(self._session(session_id))
        if s["status"] == "paused":
            return self.view(s)
        if s["status"] != "running":
            raise ApiError("NOT_ACTIVE", "공부 중일 때만 멈출 수 있어요", 409)
        at = self.at
        self.conn.execute(update(t.study_session_segments).where(
            t.study_session_segments.c.session_id == session_id, t.study_session_segments.c.ended_at.is_(None))
            .values(ended_at=at))
        values = dict(status="paused", paused_at=at)
        if s["check_prompted_at"] is not None:  # 확인 대기 중 멈춤 = 아직 공부 중이라는 뜻
            values.update(check_prompted_at=None,
                          last_confirmed_elapsed_seconds=int(self._elapsed(self._segments(session_id), at)))
        self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(**values))
        return self.view(self._session(session_id))

    def resume(self, session_id: str) -> dict:
        """멈춘 상태면 다시 시작, 공부 중이면 '아직 공부 중이에요?'에 [계속] 응답"""
        self._prepare()
        s = self._check(self._session(session_id))
        at = self.at
        if s["status"] == "paused":
            order = len(self._segments(session_id)) + 1
            self.conn.execute(t.study_session_segments.insert().values(
                id=str(uuid.uuid4()), session_id=session_id, sort_order=order, started_at=at))
            self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(
                status="running", paused_at=None))
        elif s["status"] == "running" and s["check_prompted_at"] is not None:
            self.conn.execute(update(t.study_sessions).where(t.study_sessions.c.id == session_id).values(
                check_prompted_at=None,
                last_confirmed_elapsed_seconds=int(self._elapsed(self._segments(session_id), at))))
        elif s["status"] != "running":
            raise ApiError("ENDED", "이미 끝난 공부예요", 409)
        return self.view(self._session(session_id))

    def end(self, session_id: str) -> dict:
        self._prepare()
        s = self._check(self._session(session_id))
        if s["status"] in ("running", "paused"):
            self._close(session_id, self.at, status="ended")
        return self.view(self._session(session_id))
