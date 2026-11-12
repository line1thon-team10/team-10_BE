"""익명 사용자 (NF-01). 로그인 없이 서버가 발급한 ID를 헤더 X-Anonymous-Id로 받아요."""
from fastapi import Depends, Header
from sqlalchemy import Connection, select

from app.config import ANON_HEADER
from app.core.responses import ApiError
from app.db.schema import users
from app.db.session import get_conn


def current_user(
    anonymous_id: str | None = Header(default=None, alias=ANON_HEADER),
    conn: Connection = Depends(get_conn),
) -> dict:
    if not anonymous_id:
        raise ApiError("NO_SESSION", "익명 ID가 없어요. 먼저 /api/session을 호출해 주세요", 401)
    row = conn.execute(select(users).where(users.c.id == anonymous_id)).mappings().first()
    if row is None:
        raise ApiError("UNKNOWN_SESSION", "알 수 없는 익명 ID예요. 새로 발급받아 주세요", 401)
    return dict(row)
