"""익명 세션 (NF-01) — 프론트 EP.session"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.config import DEMO_MODE
from app.core.clock import now_utc
from app.core.responses import ok
from app.repositories.user import UsersRepository
from app.db.session import get_conn
from app.seed.demo_data import DEFAULT_SEED
from app.seed.loader import load_seed

router = APIRouter(prefix="/api", tags=["세션"])


@router.post("/session")
def create_session(conn: Connection = Depends(get_conn)):
    """새 익명 ID를 발급해요. 데모 모드면 기본 시드(11/9 오후 5시)를 바로 넣어요."""
    user_id = str(uuid.uuid4())
    now = now_utc()
    UsersRepository(conn).add(dict(
        id=user_id, timezone="Asia/Seoul", onboarding_status="draft", created_at=now, updated_at=now))
    if DEMO_MODE:
        load_seed(conn, user_id, DEFAULT_SEED)
    return ok({"anonymousId": user_id})
