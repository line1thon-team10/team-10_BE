"""데모 (NF-08, NF-09) — 현재 사용자 상태와 트랜잭션 기반 초기화."""
from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.config import DEMO_MODE
from app.core.auth import current_user
from app.core.responses import ApiError, ok
from app.db.session import get_conn
from app.schemas.demo import DemoCommand, DemoResponse, DemoSeed
from app.seed.demo_data import DEFAULT_SEED
from app.services.demo import DemoService
from app.services.planner import Planner

router = APIRouter(prefix="/api/demo", tags=["데모"])


def _require_demo():
    if not DEMO_MODE:
        raise ApiError("DEMO_DISABLED", "데모 기능이 꺼져 있어요", 403)


@router.get("/state", response_model=DemoResponse)
def demo_state(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(DemoService(conn, user["id"]).state())


@router.post("/seed", response_model=DemoResponse)
def demo_seed(payload: DemoSeed, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    _require_demo()
    return ok(DemoService(conn, user["id"]).seed(payload.seed))


@router.post("/reset", response_model=DemoResponse)
def demo_reset(payload: DemoCommand | None = None, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    _require_demo()
    return ok(DemoService(conn, user["id"]).seed(DEFAULT_SEED))


@router.post("/next-day", response_model=DemoResponse)
def demo_next_day(payload: DemoCommand | None = None, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    _require_demo()
    judgment = Planner(conn, user).advance_day()
    return ok(DemoService(conn, user["id"]).state(judgment))
