"""홈 S6 · 공부 기록 S7 — 프론트 EP.today / EP.records / EP.tasks"""

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, select

from app.core.auth import current_user
from app.core.clock import now_utc
from app.core.responses import ApiError, ok
from app.db import schema as t
from app.db.session import get_conn
from app.schemas.records import FixIn, RecordIn, UUIDString
from app.repositories.record_requests import RecordRequestRepository, record_request_key
from app.services.day_notices import day_summary
from app.services.planner import Planner
from app.services.pace import PaceService
from app.services import records
from app.services.study import StudyData, unit_no

router = APIRouter(prefix="/api", tags=["홈·기록"])


@router.get("/today")
def get_today(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    user = records.lock_record_user(conn, user)
    at = now_utc()
    data = StudyData(conn, user["id"])
    if not data.plan:
        raise ApiError("NO_PLAN", "아직 계획이 없어요. 계획을 먼저 만들어 주세요", 404)
    today = records.record_date(user, at)
    prog = data.progress(today)
    pending_replan = Planner(conn, user).pending_recovery() is not None
    response = {
        "date": today.isoformat(),
        "event": day_summary(conn, user, data, today, at),
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
        "paceAlert": PaceService(conn, user).alert(),
        "replan": None,
    }
    if pending_replan:
        from app.services.replan_options import ReplanOptionsService
        engine = conn.engine
        # Persist the day-summary notice before releasing the user lock for the
        # bounded allocator. A later preview failure must not lose that receipt.
        conn.commit()
        conn.close()
        try:
            response['replan'] = ReplanOptionsService(engine).options(user['id'])
        except ApiError as error:
            if error.code != 'NO_REPLAN':
                raise
        response['paceAlert'] = None
    return ok(response)


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


@router.post("/records")
def post_record(body: RecordIn, user: dict = Depends(current_user), conn: Connection = Depends(get_conn),
                key: str | None = Depends(record_request_key)):
    user = records.lock_record_user(conn, user)
    request = RecordRequestRepository(conn, user["id"], "POST", "/api/records", key,
                                      body.model_dump(mode="json", exclude_unset=True))
    cached = request.replay()
    if cached is not None:
        return cached
    return request.save(_post_record(body, user, conn))


def _post_record(body, user, conn):
    at = now_utc()
    today = records.record_date(user, at)
    data = StudyData(conn, user["id"])
    task = records.owned_task(data, body.taskId)
    ranges = records.post_ranges(task, body)
    session_id = records.check_session(conn, task, body.sessionId, user_id=user["id"])
    before_quiz = data.quiz_progress(today)
    records.save_record(conn, task, body.result, ranges, today, session_id, user_id=user["id"], at=at)

    after_data = StudyData(conn, user["id"])
    after = after_data.progress(today)
    rate = after["rate"]

    event = day_summary(conn, user, after_data, today, at, before_quiz=before_quiz)
    if event is not None:
        return ok({"event": event})
    if body.result == "none":
        return ok({"event": None})

    unit = data.unit_of(task)
    after_quiz = after_data.quiz_progress(today)
    crossed = not before_quiz["open"] and after_quiz["open"]
    label = f"{unit_no(unit['code'])}단원 " if unit else ""
    return ok({"event": {
        "type": "toast", "reason": "rate70" if crossed else "record", "rate": rate,
        "message": f"{task.row['subject_name']} {label}한 권 쌓았어요",
        "detail": f"오늘 {rate}%" + (" · 오늘의 퀴즈가 열렸어요" if crossed else ""),
    }})


@router.patch("/tasks/{task_id}/record")
def fix_record(task_id: UUIDString, body: FixIn, user: dict = Depends(current_user), conn: Connection = Depends(get_conn),
               key: str | None = Depends(record_request_key)):
    user = records.lock_record_user(conn, user)
    request = RecordRequestRepository(conn, user["id"], "PATCH", f"/api/tasks/{task_id}/record", key,
                                      body.model_dump(mode="json", exclude_unset=True))
    cached = request.replay()
    if cached is not None:
        return cached
    return request.save(_fix_record(task_id, body, user, conn))


def _fix_record(task_id, body, user, conn):
    """기록한 뒤 끝낸 페이지 고치기 (HM-03). 이월·진행률은 다시 계산돼요"""
    at = now_utc()
    today = records.record_date(user, at)
    data = StudyData(conn, user["id"])
    task = records.owned_task(data, task_id, allow_planless_review=True)
    result, ranges = records.patch_ranges(task, body)
    records.save_record(conn, task, result, ranges, task.record["study_date"], user_id=user["id"], at=at)
    after = StudyData(conn, user["id"])
    fixed = records.owned_task(after, task_id, allow_planless_review=True)
    return ok({"taskId": task_id, "status": result, **records.record_range_view(fixed),
               "rate": after.progress(today)["rate"]})
