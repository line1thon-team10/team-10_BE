"""익명 사용자 (NF-01). 로그인 없이 서버가 발급한 ID를 헤더 X-Anonymous-Id로 받아요."""
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import Connection

from app.config import ANON_HEADER
from app.core.responses import ApiError
from app.db.session import get_conn
from app.repositories.users import UserRepository


def current_user(
    anonymous_ids: list[str] | None = Header(default=None, alias=ANON_HEADER),
    conn: Connection = Depends(get_conn),
) -> dict:
    if not anonymous_ids or anonymous_ids == [""]:
        raise ApiError("NO_SESSION", "익명 ID가 없어요. 먼저 /api/session을 호출해 주세요", 401)
    if len(anonymous_ids) != 1:
        raise ApiError("UNKNOWN_SESSION", "익명 ID는 하나만 보내 주세요", 401)
    try:
        anonymous_id = UUID(anonymous_ids[0])
    except ValueError:
        raise ApiError("UNKNOWN_SESSION", "익명 ID의 형식이 올바르지 않아요. 새로 발급받아 주세요", 401) from None
    user = UserRepository(conn).get(anonymous_id)
    if user is None:
        raise ApiError("UNKNOWN_SESSION", "알 수 없는 익명 ID예요. 새로 발급받아 주세요", 401)
    return user.to_mapping()
