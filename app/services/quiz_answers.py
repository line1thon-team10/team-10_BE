"""Answer and job writes share the request's outer transaction."""
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.quiz_answers import QuizAnswerRepository, canonical_hash
from app.schemas.quiz_answers import SaveAnswerIn
from app.services.study import OPEN_RATE, StudyData


def lock_quiz_user(conn, user: dict) -> dict:
    try:
        fresh = lock_user(conn, UUID(user["id"]))
    except UserBusyError as exc:
        raise ApiError("REQUEST_IN_PROGRESS", "이전 요청을 처리 중이에요. 잠시 후 다시 시도해 주세요", 409) from exc
    if fresh is None:
        raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
    return fresh.to_mapping()


def save_answer(conn, user: dict, quiz_id: str, item_id: str, body: SaveAnswerIn) -> dict:
    user = lock_quiz_user(conn, user)
    now = now_utc()
    today = user.get("demo_date") or now.astimezone(ZoneInfo(user.get("timezone") or "Asia/Seoul")).date()
    answers = QuizAnswerRepository(conn)
    quiz = answers.quiz(user["id"], quiz_id)
    item = answers.item(user["id"], quiz["id"], item_id)
    current = answers.current(user["id"], quiz["id"], item["id"])
    payload = body.model_dump(exclude={"revision"})
    # A receipt is a replay, even if eligibility or lifecycle changed since the
    # original commit. A previously unseen no-op still needs an open quiz.
    received = conn.scalar(select(t.quiz_answer_receipts.c.response_answer_id).where(
        t.quiz_answer_receipts.c.quiz_item_id == item["id"],
        t.quiz_answer_receipts.c.request_revision == body.revision,
        t.quiz_answer_receipts.c.answer_hash == canonical_hash(payload))) if (
            body.revision <= (current["revision"] if current else 0)) else None
    if received is None:
        if (quiz["status"] in ("submitted", "carried_over") or quiz["finalized_at"] is not None
                or answers.submission(user["id"], quiz["id"]) is not None):
            raise ApiError("QUIZ_FINALIZED", "이미 봉인한 퀴즈의 답은 바꿀 수 없어요", 409)
        if quiz["status"] not in ("ready", "in_progress"):
            raise ApiError("NO_QUIZ", "아직 풀 수 있는 퀴즈가 아니에요", 404)
        if quiz["type"] == "daily":
            progress = StudyData(conn, user["id"]).quiz_progress(today)
            if not progress["subjectOpen"].get(quiz["subject_id"], False):
                raise ApiError("QUIZ_LOCKED", f"오늘 계획의 {OPEN_RATE}%를 넘기면 열려요", 403)
    saved = answers.save(user["id"], quiz["id"], item["id"], revision=body.revision,
                         now=now, answered_date=today, **payload)
    if saved.created:
        # Never re-enqueue a receipt's older revision. The original answer and
        # its job committed together, and only a new revision needs a new job.
        GradingJobRepository(conn).enqueue(user["id"], quiz["id"], item["id"],
                                           revision=saved.answer["revision"], now=now)
    return {"quizId": quiz["id"], "questionId": item["id"], "saved": True,
            "revision": saved.answer["revision"]}
