"""Record mutations and existing learning-pass evidence use one transaction."""
from uuid import UUID

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.records import RecordRepository
from app.services.learning_evidence import revalidate_record_passes
from app.services.study import StudyData, Task

RESULT_TO_DB = {"done": "completed", "partial": "partial", "none": "missed"}


def lock_record_user(conn, user: dict) -> dict:
    try:
        fresh = lock_user(conn, UUID(user["id"]))
    except UserBusyError as exc:
        raise ApiError("REQUEST_IN_PROGRESS", "다른 요청이 처리 중이에요. 잠시 후 다시 시도해 주세요", 409) from exc
    if fresh is None:
        raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
    return fresh.to_mapping()


def find_task(data: StudyData, task_id: str) -> Task:
    task = next((item for item in data.tasks if item.id == task_id), None)
    if task is None:
        raise ApiError("TASK_NOT_FOUND", "태스크를 찾지 못했어요", 404)
    return task


def ranges_until(task: Task, to_page: int) -> list[dict]:
    ranges = []
    for item in task.paged:
        if item["start_page"] > to_page:
            break
        ranges.append({"unit_id": item["unit_id"], "start_page": item["start_page"],
                       "end_page": min(item["end_page"], to_page)})
    return ranges


def save_record(conn, task: Task, result: str, ranges: list[dict], study_date, session_id=None,
                *, user_id: str) -> None:
    at = now_utc()
    status = RESULT_TO_DB[result]
    if result == "partial" and task.last is not None and ranges and ranges[-1]["end_page"] >= task.last:
        status = "completed"
    record_id = RecordRepository(conn).replace(
        task, RESULT_TO_DB[result], status, ranges, study_date, session_id, at)
    revalidate_record_passes(conn, user_id, record_id, at)


def check_session(conn, task: Task, session_id: str | None) -> str | None:
    if not session_id:
        return None
    repository = RecordRepository(conn)
    session = repository.session(session_id)
    if not session or session["task_id"] != task.id:
        raise ApiError("SESSION_NOT_FOUND", "이 태스크의 집중 기록을 찾지 못했어요", 404)
    if session["status"] not in ("ended", "auto_stopped"):
        raise ApiError("SESSION_RUNNING", "집중 모드를 먼저 끝내 주세요", 409)
    used = repository.session_record(session_id)
    if used and (task.record is None or used != task.record["id"]):
        raise ApiError("SESSION_USED", "이미 기록에 붙은 집중 시간이에요", 409)
    return session_id
