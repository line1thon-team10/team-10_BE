"""데모 (NF-08, NF-09) — 프론트 EP.demoState / demoSeed / demoReset / demoNextDay"""
from fastapi import APIRouter, Body, Depends
from sqlalchemy import Connection, func, select

from app.config import DEMO_MODE
from app.core.auth import current_user
from app.core.clock import today_for
from app.core.responses import ApiError, ok
from app.db.schema import plans, subjects, users
from app.db.session import get_conn
from app.seed.demo_data import DEFAULT_SEED, SEED_DATES, SEEDS
from app.seed.loader import load_seed

router = APIRouter(prefix="/api/demo", tags=["데모"])


def _require_demo():
    if not DEMO_MODE:
        raise ApiError("DEMO_DISABLED", "데모 기능이 꺼져 있어요", 403)


def _seed_name(conn: Connection, user: dict) -> str | None:
    """지금 상태가 어떤 시드에서 왔는지 (날짜·계획 유무로 추정)"""
    has_plan = conn.execute(select(func.count()).select_from(plans).where(plans.c.user_id == user["id"])).scalar()
    for name, day in SEED_DATES.items():
        if user.get("demo_date") == day and bool(has_plan) == (name != "empty"):
            return name if name != "overdue" else None  # day9/overdue는 날짜가 같아서 구분 안 함
    return None


def _state(conn: Connection, user: dict, judgment: str | None = None) -> dict:
    today = today_for(user)
    rows = conn.execute(
        select(subjects.c.id, subjects.c.name, subjects.c.exam_date)
        .where(subjects.c.user_id == user["id"], subjects.c.status != "archived", subjects.c.exam_date.is_not(None))
        .order_by(subjects.c.exam_date)
    ).all()
    upcoming = [r for r in rows if r.exam_date >= today] or rows[-1:]
    first = upcoming[0] if upcoming else None
    has_plan = conn.execute(select(func.count()).select_from(plans).where(plans.c.user_id == user["id"])).scalar()
    return {
        "today": today.isoformat(),
        "firstExam": {"subjectId": first.id, "name": first.name, "examDate": first.exam_date.isoformat()}
        if first else None,
        "seed": user.get("_seed") or _seed_name(conn, user),
        "seeds": SEEDS,
        "hasPlan": bool(has_plan),
        "judgment": judgment,
    }


def _reload_user(conn: Connection, user_id: str) -> dict:
    return dict(conn.execute(select(users).where(users.c.id == user_id)).mappings().one())


@router.get("/state")
def demo_state(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return ok(_state(conn, user))


@router.post("/seed")
def demo_seed(seed: str = Body(..., embed=True), user: dict = Depends(current_user),
              conn: Connection = Depends(get_conn)):
    _require_demo()
    if seed not in SEEDS:
        raise ApiError("BAD_SEED", "없는 시드예요")
    load_seed(conn, user["id"], seed)
    fresh = _reload_user(conn, user["id"]) | {"_seed": seed}
    return ok(_state(conn, fresh))


@router.post("/reset")
def demo_reset(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    _require_demo()
    load_seed(conn, user["id"], DEFAULT_SEED)
    fresh = _reload_user(conn, user["id"]) | {"_seed": DEFAULT_SEED}
    return ok(_state(conn, fresh))


@router.post("/next-day")
def demo_next_day(user: dict = Depends(current_user)):
    """[다음 날로] = 자정 판정(RC-01·02) + 이월/분배안. 백엔드 A 담당이라 아직 비어 있어요."""
    _require_demo()
    raise ApiError("NOT_IMPLEMENTED", "다음 날로(자정 판정)는 아직 구현 전이에요 (백엔드 A)", 501)
