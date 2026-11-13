"""홈 S6 · 공부 기록 S7 — 프론트 EP.today / EP.records / EP.tasks"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, select, update

from app.core.auth import current_user
from app.core.clock import now_utc, today_for
from app.core.responses import ApiError, ok
from app.db import schema as t
from app.db.session import get_conn
from app.repositories.locking import UserBusyError, lock_user
from app.services.planner import Planner
from app.services.study import OPEN_RATE, StudyData, Task, unit_no

router = APIRouter(prefix="/api", tags=["홈·기록"])

RESULT_TO_DB = {"done": "completed", "partial": "partial", "none": "missed"}


def _lock_record_user(conn: Connection, user: dict) -> dict:
    """demo/focus와 같은 잠금에서 기록과 파생 근거를 원자적으로 바꾼다."""
    try:
        fresh = lock_user(conn, uuid.UUID(user["id"]))
    except UserBusyError as exc:
        raise ApiError("REQUEST_IN_PROGRESS", "다른 요청이 처리 중이에요. 잠시 후 다시 시도해 주세요", 409) from exc
    if fresh is None:
        raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
    return fresh.to_mapping()


@router.get("/today")
def get_today(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    data = StudyData(conn, user["id"])
    if not data.plan:
        raise ApiError("NO_PLAN", "아직 계획이 없어요. 계획을 먼저 만들어 주세요", 404)
    today = today_for(user)
    prog = data.progress(today)
    return ok({
        "date": today.isoformat(),
        "subjects": [{"id": s["id"], "name": s["name"], "color": s["color"],
                      "examDate": s["exam_date"].isoformat() if s["exam_date"] else None} for s in data.subjects],
        "tasks": [data.task_view(x) for x in data.tasks_on(today)],
        "progress": {k: prog[k] for k in ("doneCount", "totalCount", "donePages", "totalPages", "rate")},
        "subjectRates": prog["subjectRates"],
        "quiz": data.quiz_view(today, prog),
        "exams": [{"subjectId": s["id"],
                   "dday": (s["exam_date"] - today).days if s["exam_date"] else None,
                   "readiness": data.readiness(s)["readiness"]} for s in data.subjects],
        "recentDays": data.recent_days(today),
        "notice": _notice(conn, user, today),
        "tutorSuggestions": _tutor_suggestions(data, today),
        "paceAlert": None,    # 속도 보정 알림 (PL-07) — 측정 기록 3일치가 쌓이면
        "replan": Planner(conn, user).replan_view(),
    })


def _tutor_suggestions(data: StudyData, today) -> list[str]:
    """오늘의 실제 단원에서 질문을 만든다. 옛 데모 과목을 고정하지 않는다."""
    suggestions = []
    for subject in data.subjects:
        task = next((task for task in data.tasks_on(today)
                     if task.row["subject_id"] == subject["id"] and data.unit_of(task)), None)
        if task:
            unit = data.unit_of(task)
            suggestions.append(f"{subject['name']}의 {unit['title']} 핵심 개념을 설명해 줘.")
    return suggestions[:3]


def _notice(conn: Connection, user: dict, today) -> dict | None:
    """오늘 접속하면 보여줄 안내: 이월(S6-2) 또는 분배안 적용 뒤(회복)"""
    rows = conn.execute(select(t.user_notices).where(
        t.user_notices.c.user_id == user["id"], t.user_notices.c.notice_date == today,
        t.user_notices.c.notice_type.in_(["carry", "recovery"])).order_by(t.user_notices.c.created_at.desc())).mappings().all()
    for r in rows:
        if r["notice_type"] == "recovery":
            return {"type": "replanned", "key": r["payload"].get("key"), "items": []}
    for r in rows:
        return {"type": "carry", "items": r["payload"].get("items", [])}
    return None


class RecordIn(BaseModel):
    taskId: str
    result: Literal["done", "partial", "none"]
    toPage: int | None = None
    sessionId: str | None = None


def _find_task(data: StudyData, task_id: str) -> Task:
    task = next((x for x in data.tasks if x.id == task_id), None)
    if task is None:
        raise ApiError("TASK_NOT_FOUND", "태스크를 찾지 못했어요", 404)
    return task


def _ranges_until(task: Task, to_page: int) -> list[dict]:
    """계획 범위를 앞에서부터 to_page까지 자른 범위 (부분 완료)"""
    out = []
    for r in task.paged:
        if r["start_page"] > to_page:
            break
        out.append({"unit_id": r["unit_id"], "start_page": r["start_page"], "end_page": min(r["end_page"], to_page)})
    return out


def _save_record(conn: Connection, task: Task, result: str, ranges: list[dict], study_date, session_id=None) -> None:
    now = now_utc()
    if task.record:
        rec_id = task.record["id"]
        values = {"result": RESULT_TO_DB[result], "updated_at": now}
        if session_id:
            values["session_id"] = session_id
        conn.execute(update(t.study_records).where(t.study_records.c.id == rec_id).values(**values))
        conn.execute(t.study_record_ranges.delete().where(t.study_record_ranges.c.record_id == rec_id))
    else:
        rec_id = str(uuid.uuid4())
        conn.execute(t.study_records.insert().values(
            id=rec_id, task_id=task.id, session_id=session_id, result=RESULT_TO_DB[result],
            recorded_at=now, study_date=study_date, updated_at=now))
    for r in ranges:
        conn.execute(t.study_record_ranges.insert().values(
            id=str(uuid.uuid4()), record_id=rec_id, unit_id=r["unit_id"],
            start_page=r["start_page"], end_page=r["end_page"]))
    status = RESULT_TO_DB[result]
    if result == "partial" and task.last is not None and ranges and ranges[-1]["end_page"] >= task.last:
        status = "completed"
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task.id).values(status=status))
    if status == "completed":
        conn.execute(update(t.study_blocks).where(t.study_blocks.c.id == task.row["block_id"],
                                                  t.study_blocks.c.stacked_at.is_(None)).values(stacked_at=now))


def _check_session(conn: Connection, task: Task, session_id: str | None) -> str | None:
    if not session_id:
        return None
    s = conn.execute(select(t.study_sessions).where(t.study_sessions.c.id == session_id)).mappings().first()
    if not s or s["task_id"] != task.id:
        raise ApiError("SESSION_NOT_FOUND", "이 태스크의 집중 기록을 찾지 못했어요", 404)
    if s["status"] not in ("ended", "auto_stopped"):
        raise ApiError("SESSION_RUNNING", "집중 모드를 먼저 끝내 주세요", 409)
    used = conn.execute(select(t.study_records.c.id).where(t.study_records.c.session_id == session_id)).first()
    if used:
        raise ApiError("SESSION_USED", "이미 기록에 붙은 집중 시간이에요", 409)
    return session_id


@router.post("/records")
def post_record(body: RecordIn, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    user = _lock_record_user(conn, user)
    today = today_for(user)
    data = StudyData(conn, user["id"])
    task = _find_task(data, body.taskId)
    if body.result == "partial":
        if body.toPage is None or task.first is None or not (task.first <= body.toPage <= task.last):
            raise ApiError("BAD_PAGE", f"p.{task.first}부터 p.{task.last} 사이로 적어 주세요")
        ranges = _ranges_until(task, body.toPage)
    elif body.result == "done":
        ranges = [{"unit_id": r["unit_id"], "start_page": r["start_page"], "end_page": r["end_page"]}
                  for r in task.paged]
    else:
        ranges = []
    session_id = _check_session(conn, task, body.sessionId)
    before = data.progress(today)
    _save_record(conn, task, body.result, ranges, today, session_id)

    after_data = StudyData(conn, user["id"])
    after = after_data.progress(today)
    rate = after["rate"]

    # 하루 마무리 박스 (BK-03): 오늘 태스크를 다 기록했고 70% 이상이면 하루 한 번
    # TODO(백엔드 A): 23:30이 지났을 때 조건은 데모 시각을 정한 뒤 추가
    dedupe = f"day_summary:{today.isoformat()}"
    shown = conn.execute(select(t.user_notices.c.id).where(
        t.user_notices.c.user_id == user["id"], t.user_notices.c.dedupe_key == dedupe)).first()
    if after["recordedAll"] and rate >= OPEN_RATE and not shown:
        counts = after_data.quiz_counts(today)
        was = {sid for sid, r in before["subjectRates"].items() if r >= OPEN_RATE}
        opened = [{"subjectId": sid, "count": counts.get(sid, 0)}
                  for sid, r in after["subjectRates"].items() if r >= OPEN_RATE and sid not in was]
        event = {"type": "box", "reason": "dayClose", "rate": rate,
                 "recordedCount": sum(1 for x in after_data.tasks_on(today) if x.row["status"] != "planned"),
                 "totalCount": after["totalCount"], "donePages": after["donePages"],
                 "totalPages": after["totalPages"], "opened": opened}
        conn.execute(t.user_notices.insert().values(
            id=str(uuid.uuid4()), user_id=user["id"], dedupe_key=dedupe, notice_type="day_summary",
            notice_date=today, status="shown", payload={"percent": rate}, created_at=now_utc(), shown_at=now_utc()))
        return ok({"event": event})
    if body.result == "none":
        return ok({"event": None})

    subject = data.subject_by_id[task.row["subject_id"]]
    unit = data.unit_of(task)
    crossed = before["rate"] < OPEN_RATE <= rate
    label = f"{unit_no(unit['code'])}단원 " if unit else ""
    return ok({"event": {
        "type": "toast", "reason": "rate70" if crossed else "record", "rate": rate,
        "message": f"{subject['name']} {label}한 권 쌓았어요",
        "detail": f"오늘 {rate}%" + (" · 오늘의 퀴즈가 열렸어요" if crossed else ""),
    }})


class FixIn(BaseModel):
    toPage: int


@router.patch("/tasks/{task_id}/record")
def fix_record(task_id: str, body: FixIn, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """기록한 뒤 끝낸 페이지 고치기 (HM-03). 이월·진행률은 다시 계산돼요"""
    user = _lock_record_user(conn, user)
    today = today_for(user)
    data = StudyData(conn, user["id"])
    task = _find_task(data, task_id)
    if task.row["status"] not in ("completed", "partial"):
        raise ApiError("NOT_RECORDED", "아직 기록하지 않은 태스크예요")
    if task.first is None or not (task.first <= body.toPage <= task.last):
        raise ApiError("BAD_PAGE", f"p.{task.first}부터 p.{task.last} 사이로 적어 주세요")
    result = "done" if body.toPage >= task.last else "partial"
    _save_record(conn, task, result, _ranges_until(task, body.toPage), task.record["study_date"])
    after = StudyData(conn, user["id"])
    fixed = _find_task(after, task_id)
    return ok({"taskId": task_id, "status": after.task_view(fixed)["status"], "doneTo": fixed.done_to,
               "rate": after.progress(today)["rate"]})
