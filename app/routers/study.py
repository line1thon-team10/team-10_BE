"""집중 모드 S12 · 공부 끝 S12-1 (HM-07~10) — 프론트 EP.sessions"""
from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.core.auth import current_user
from app.core.clock import today_for
from app.core.responses import ok
from app.db.session import get_conn
from app.services.focus import FocusService
from app.schemas.focus import FocusCommand, FocusStart, FocusResponse

router = APIRouter(prefix="/api/study/sessions", tags=["집중 모드"])


def _svc(user: dict, conn: Connection) -> FocusService:
    return FocusService(conn, user, today_for(user))


@router.post("", response_model=FocusResponse)
def start(body: FocusStart, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """태스크 [시작]. 같은 태스크가 이미 진행 중이면 그대로 이어서 돌려줘요"""
    return ok(_svc(user, conn).start(str(body.taskId)))


@router.get("/active", response_model=FocusResponse)
def active(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """진행 중(공부 중·멈춤)인 세션. 없으면 null"""
    return ok(_svc(user, conn).active())


@router.get("/{session_id}", response_model=FocusResponse)
def get_one(session_id: UUID, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    svc = _svc(user, conn)
    return ok(svc.get(str(session_id)))


@router.post("/{session_id}/pause", response_model=FocusResponse)
def pause(session_id: UUID, body: FocusCommand | None = None, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(_svc(user, conn).pause(str(session_id)))


@router.post("/{session_id}/resume", response_model=FocusResponse)
def resume(session_id: UUID, body: FocusCommand | None = None, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """다시 시작, 또는 '아직 공부 중이에요?'에 계속"""
    return ok(_svc(user, conn).resume(str(session_id)))


@router.post("/{session_id}/end", response_model=FocusResponse)
def end(session_id: UUID, body: FocusCommand | None = None, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    """[종료] → 이번 시간 + 오늘 총 + 과목 누적 (S12-1)"""
    return ok(_svc(user, conn).end(str(session_id)))
