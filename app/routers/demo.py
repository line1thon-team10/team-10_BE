"""데모 (NF-08, NF-09) — 현재 사용자 상태와 트랜잭션 기반 초기화."""
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy import Connection

from app.config import DEMO_MODE
from app.core.auth import current_user
from app.core.responses import ApiError, ok
from app.db.session import get_conn
from app.schemas.demo import DemoCommand, DemoResponse, DemoSeed
from app.seed.demo_data import DEFAULT_SEED
from app.services.demo import DemoService

router = APIRouter(prefix="/api/demo", tags=["데모"])


def _request_key(values: list[str] | None = Header(default=None, alias="Idempotency-Key")) -> UUID | None:
    if values is None:
        return None
    try:
        if len(values) != 1:
            raise ValueError
        return UUID(values[0])
    except ValueError:
        raise ApiError("INVALID_INPUT", "Idempotency-Key는 UUID 하나만 보내 주세요", 422) from None


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
def demo_next_day(payload: DemoCommand | None = None, user: dict = Depends(current_user), conn: Connection = Depends(get_conn),
                  key: UUID | None = Depends(_request_key)):
    _require_demo()
    return ok(DemoService(conn, user["id"]).next_day(key))
