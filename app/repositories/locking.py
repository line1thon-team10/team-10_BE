"""한 사용자 상태를 변경하는 요청들의 공통 트랜잭션 잠금."""
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.db.schema import users
from app.repositories.users import UserRepository


class UserBusyError(Exception):
    pass


def lock_user(connection, user_id: UUID):
    try:
        if connection.dialect.name == "postgresql":
            connection.execute(select(users.c.id).where(users.c.id == str(user_id))
                               .with_for_update(nowait=True)).first()
        else:
            connection.execute(update(users).where(users.c.id == str(user_id)).values(id=str(user_id)))
    except DBAPIError as error:
        code = getattr(error.orig, "sqlite_errorcode", 0)
        if getattr(error.orig, "sqlstate", None) == "55P03" or (code & 255) in (5, 6):
            raise UserBusyError from error
        raise
    return UserRepository(connection).get(user_id)
