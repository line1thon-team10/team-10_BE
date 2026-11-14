"""오늘의 퀴즈 S8 · 결과 S8-2 · 더 풀기 (QZ) — 프론트 EP.quizToday / EP.quiz"""
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.clock import today_for
from app.core.responses import ApiError, ok
from app.db.session import get_conn
from app.schemas.quiz import SubmitIn
from app.services.quiz import QuizService, DIFF_KO
from app.services.quiz_selection import QuizSelection
from app.services.quiz_finalization import QuizFinalizationService
from app.services.study import OPEN_RATE

router = APIRouter(prefix="/api/quiz", tags=["퀴즈"])


@router.get("/today")
def quiz_today(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """과목별 오늘의 퀴즈 카드 (S8-1): 문항 수, 열림/잠김(70%), 구성, 이미 풀었으면 결과"""
    today = today_for(user)
    svc = QuizService(conn, user, today)
    data = svc.data
    prog = data.quiz_progress(today)
    counts = {}
    selection = QuizSelection(conn, user, today)
    subjects = []
    for sid in prog["subjectRates"]:
        s = data.subject_by_id[sid]
        sessions = svc.daily_sessions(sid)
        current = next((s for s in sessions if s['status'] in ('ready','in_progress')), sessions[-1] if sessions else None)
        items = svc.items(current["id"]) if current else []
        if not selection.has_scope(sid):
            counts[sid] = 0
        elif current:
            counts[sid] = len(items)
        else:
            try:
                scopes = selection.completed(sid)
                difficulty = selection.difficulty(scopes, None)
                chosen, _ = selection.fresh(sid, scopes, difficulty, [])
                counts[sid] = len(chosen) + len(selection.reviews(sid))
            except ApiError as error:
                if error.code not in ('NO_QUESTIONS', 'INVALID_INPUT'): raise
                counts[sid] = 0
        _, lines = svc.composition(items) if items else (None, [])
        done = current if current and current["status"] == "submitted" else None
        result = None
        if done:
            summary = svc.result_view(done)["summary"]
            result = {"correct": summary["correct"], "total": summary["total"]}
        rate = prog["subjectRates"].get(sid, 0)
        subjects.append({
            "subjectId": sid, "name": s["name"], "color": s["color"], "count": counts.get(sid, 0), "rate": rate,
            "open": prog["subjectOpen"][sid], "hasQuestions": counts.get(sid, 0) > 0,
            "lines": lines, "round": svc.round_of(items) if items else None,
            "difficulty": DIFF_KO.get(current["selected_difficulty"]) if current else None,
            "essayCount": sum(1 for x in items if x["type"] == "essay") if items else None, "result": result,
        })
    return ok({"date": today.isoformat(), "rate": prog["rate"], "open": prog["open"],
               "openRate": OPEN_RATE, "subjects": subjects})


def _check_open(svc: QuizService, session: dict) -> None:
    """70% 미만이면 잠김 (QZ-02). 더 풀기·문제 뽑기는 예외"""
    if session["type"] != "daily" or session["status"] == "submitted":
        return
    prog = svc.data.quiz_progress(svc.today)
    if not prog["subjectOpen"].get(session["subject_id"], False):
        raise ApiError("QUIZ_LOCKED", f"오늘 계획의 {OPEN_RATE}%를 넘기면 열려요", 403)


@router.get("/{key}")
def quiz_set(key: UUID, saved: int = 0, quizId: UUID | None = None, difficulty: Literal["low", "mid", "high"] | None = None,
            user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """문항 (S8). key = 과목 ID(오늘 퀴즈) 또는 퀴즈 ID(더 풀기). 정답·해설은 보내지 않아요"""
    svc = QuizService(conn, user, today_for(user))
    session = svc.session(str(key), quiz_id=str(quizId) if quizId is not None else None, difficulty=difficulty)
    _check_open(svc, session)
    return ok(svc.set_view(session, saved=bool(saved)))


@router.post("/{key}/submit")
def quiz_submit(key: UUID, body: SubmitIn, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """제출 → 채점(서술형은 AI) → 단원 점수·오답 노트 반영 → 결과 (S8-2, P3)"""
    engine = conn.engine
    # current_user performed reads only. Return its connection before the
    # finalizer commits inputs and executes grading outside transactions.
    conn.close()
    return ok(QuizFinalizationService(engine).submit(user["id"], str(key), body))


@router.get("/{key}/result")
def quiz_result(key: UUID, quizId: UUID | None = None,
                user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    svc = QuizService(conn, user, today_for(user))
    return ok(svc.result_view(svc.result_session(str(key), quiz_id=str(quizId) if quizId is not None else None)))


class MoreIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    unitId: UUID
    difficulty: Literal['low', 'mid', 'high'] | None = None


@router.post('/{subject_id}/more')
def quiz_more(subject_id: UUID, body: MoreIn, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    from app.services.extra_quiz import create_extra
    return ok(create_extra(conn, user, today_for(user), str(subject_id), str(body.unitId), body.difficulty))
