"""익명 세션 (NF-01) — 프론트 EP.session"""
from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.config import DEMO_MODE
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.session import SessionCreate, SessionResponse
from app.services.session import SessionService

router = APIRouter(prefix="/api", tags=["세션"])


@router.post("/session", response_model=SessionResponse)
def create_session(payload: SessionCreate | None = None, conn: Connection = Depends(get_conn)):
    """새 익명 ID를 발급해요. 데모 모드면 기본 시드(11/9 오후 5시)를 바로 넣어요."""
    user_id = SessionService(conn).create(demo_mode=DEMO_MODE)
    return ok({"anonymousId": user_id})
