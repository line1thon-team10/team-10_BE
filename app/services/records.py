"""Record mutations and existing learning-pass evidence use one transaction."""
from collections import defaultdict
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.domain.ranges import PageSpan, intersect, page_count
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.records import RecordRepository
from app.services.learning_evidence import revalidate_record_passes
from app.services.learning_passes import create_initial_passes, create_remedial_passes
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


def owned_task(data: StudyData, task_id: str) -> Task:
    repository = RecordRepository(data.conn)
    context = repository.task_context(task_id, data.user_id)
    if context is None:
        raise ApiError("TASK_NOT_FOUND", "태스크를 찾지 못했어요", 404)
    task = find_task(data, task_id)
    ranges = repository.ranges(task_id)
    if any(item["range_subject_id"] != task.row["subject_id"] or (
        item["concept_id"] is not None and item["concept_unit_id"] != item["unit_id"]
    ) for item in ranges):
        raise ApiError("TASK_NOT_FOUND", "태스크의 자료 범위를 찾지 못했어요", 404)
    task.ranges = ranges
    task.row["subject_name"] = context["subject_name"]
    return task


def record_date(user, at):
    return user.get("demo_date") or at.astimezone(ZoneInfo(user.get("timezone") or "Asia/Seoul")).date()


def _invalid(message):
    return ApiError("INVALID_INPUT", message, 422)


def requested_ranges(task: Task, body) -> list[dict]:
    """Resolve range IDs only against this task, without retaining omitted ranges."""
    if body.toPage is not None:
        if len(task.ranges) != 1 or not task.paged:
            raise _invalid("여러 범위는 completedRanges로 기록해 주세요")
        selections = [(task.ranges[0], body.toPage)]
    elif body.completedRanges is not None:
        by_id = {item["id"]: item for item in task.paged}
        selections = []
        for selected in body.completedRanges:
            if selected.rangeId not in by_id:
                raise _invalid("이 태스크에 속한 페이지 범위를 보내 주세요")
            selections.append((by_id[selected.rangeId], selected.toPage))
    else:
        raise _invalid("완료한 범위를 보내 주세요")
    ranges = []
    for item, end in selections:
        if not item["start_page"] <= end <= item["end_page"]:
            raise _invalid("완료 페이지가 태스크 범위를 벗어났어요")
        ranges.append({"unit_id": item["unit_id"], "material_id": item["material_id"],
                       "start_page": item["start_page"], "end_page": end})
    return ranges


def range_pages(ranges) -> int:
    return page_count(PageSpan(item["material_id"], item["start_page"], item["end_page"])
                      for item in ranges if item["start_page"] is not None)


def post_ranges(task: Task, body) -> list[dict]:
    if not task.paged and body.result == "partial":
        raise ApiError("BAD_PAGE", "페이지 없는 태스크는 완료 또는 미완료로 기록해 주세요")
    if body.toPage is not None or body.completedRanges is not None:
        ranges = requested_ranges(task, body)
    elif body.result == "done":
        ranges = task.paged
    elif body.result == "none":
        ranges = []
    else:
        raise _invalid("부분 완료에는 완료한 범위가 필요해요")
    total, completed = range_pages(task.paged), range_pages(ranges)
    if total:
        actual = "none" if completed == 0 else "done" if completed == total else "partial"
        if body.result != actual:
            raise _invalid("완료한 페이지 수에 맞는 결과를 보내 주세요")
    return ranges


def patch_ranges(task: Task, body) -> tuple[str, list[dict]]:
    if task.record is None:
        raise ApiError("NOT_RECORDED", "아직 기록하지 않은 태스크예요")
    ranges = requested_ranges(task, body)
    total, completed = range_pages(task.paged), range_pages(ranges)
    result = "none" if completed == 0 else "done" if completed == total else "partial"
    return result, ranges


def record_range_view(task: Task) -> dict:
    """The correction response keeps unit evidence and PDF totals separate."""
    by_unit = defaultdict(list)
    for item in task.done_ranges:
        if item["start_page"] is not None and item["end_page"] is not None:
            by_unit[item["unit_id"]].append(PageSpan(item["unit_id"], item["start_page"], item["end_page"]))
    ranges, completed_pages = [], []
    for item in task.paged:
        completed = intersect([PageSpan(item["unit_id"], item["start_page"], item["end_page"])],
                              by_unit[item["unit_id"]])
        # A later completed island cannot claim the uncompleted prefix is done.
        done_to = completed[0].end if completed and completed[0].start == item["start_page"] else None
        completed_pages.extend(PageSpan(item["material_id"], span.start, span.end) for span in completed)
        ranges.append({"id": item["id"], "materialId": item["material_id"], "unitId": item["unit_id"],
                       "conceptId": item["concept_id"], "from": item["start_page"], "to": item["end_page"],
                       "doneTo": done_to})
    return {"ranges": ranges, "donePages": page_count(completed_pages),
            "doneTo": ranges[0]["doneTo"] if len(task.ranges) == 1 and ranges else None}


def save_record(conn, task: Task, result: str, ranges: list[dict], study_date, session_id=None,
                *, user_id: str, at=None) -> None:
    at = at or now_utc()
    status = RESULT_TO_DB[result]
    record_id = RecordRepository(conn).replace(
        task, RESULT_TO_DB[result], status, ranges, study_date, session_id, at)
    revalidate_record_passes(conn, user_id, record_id, at)
    create_initial_passes(conn, user_id, task, record_id, at)
    create_remedial_passes(conn, user_id, task, record_id, at)


def check_session(conn, task: Task, session_id: str | None, *, user_id: str) -> str | None:
    if not session_id:
        return None
    repository = RecordRepository(conn)
    session = repository.session(session_id, user_id)
    if not session or session["task_id"] != task.id:
        raise ApiError("SESSION_NOT_FOUND", "이 태스크의 집중 기록을 찾지 못했어요", 404)
    if session["status"] not in ("ended", "auto_stopped"):
        raise ApiError("SESSION_RUNNING", "집중 모드를 먼저 끝내 주세요", 409)
    used = repository.session_record(session_id)
    if used and (task.record is None or used != task.record["id"]):
        raise ApiError("SESSION_USED", "이미 기록에 붙은 집중 시간이에요", 409)
    return session_id
