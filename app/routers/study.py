"""집중 모드 S12 · 공부 끝 S12-1 (HM-07~10) — 프론트 EP.sessions"""
from fastapi import APIRouter, Body, Depends
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.clock import today_for
from app.core.responses import ok
from app.db.session import get_conn
from app.services.focus import FocusService

router = APIRouter(prefix="/api/study/sessions", tags=["집중 모드"])


def _svc(user: dict, conn: Connection) -> FocusService:
    return FocusService(conn, user, today_for(user))


@router.post("")
def start(taskId: str = Body(..., embed=True), user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """태스크 [시작]. 같은 태스크가 이미 진행 중이면 그대로 이어서 돌려줘요"""
    return ok(_svc(user, conn).start(taskId))


@router.get("/active")
def active(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """진행 중(공부 중·멈춤)인 세션. 없으면 null"""
    return ok(_svc(user, conn).active())


@router.get("/{session_id}")
def get_one(session_id: str, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    svc = _svc(user, conn)
    return ok(svc.view(svc._session(session_id)))


@router.post("/{session_id}/pause")
def pause(session_id: str, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(_svc(user, conn).pause(session_id))


@router.post("/{session_id}/resume")
def resume(session_id: str, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """다시 시작, 또는 '아직 공부 중이에요?'에 계속"""
    return ok(_svc(user, conn).resume(session_id))


@router.post("/{session_id}/end")
def end(session_id: str, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """[종료] → 이번 시간 + 오늘 총 + 과목 누적 (S12-1)"""
    return ok(_svc(user, conn).end(session_id))
