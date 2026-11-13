from uuid import UUID

from sqlalchemy import Connection, func, select, update
from sqlalchemy.exc import DBAPIError

from app.core.clock import today_for
from app.db.schema import plans, subjects, users
from app.repositories.users import UserRepository


class DemoBusyError(Exception):
    pass


class DemoRepository:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.users = UserRepository(connection)

    def lock_user(self, user_id: UUID):
        try:
            if self.connection.dialect.name == "postgresql":
                self.connection.execute(select(users.c.id).where(users.c.id == str(user_id))
                                        .with_for_update(nowait=True)).first()
            else:
                # SQLite에는 행 잠금이 없다. write lock을 먼저 얻고 상태를 다시 읽는다.
                self.connection.execute(update(users).where(users.c.id == str(user_id)).values(id=str(user_id)))
        except DBAPIError as error:
            code = getattr(error.orig, "sqlite_errorcode", 0)
            if getattr(error.orig, "sqlstate", None) == "55P03" or (code & 255) in (5, 6):
                raise DemoBusyError from error
            raise
        return self.users.get(user_id)

    def has_plan(self, user_id: UUID) -> bool:
        return bool(self.connection.scalar(select(func.count()).select_from(plans).where(plans.c.user_id == str(user_id))))

    def first_exam(self, user):
        today = today_for(user.to_mapping())
        row = self.connection.execute(
            select(subjects.c.id, subjects.c.name, subjects.c.exam_date)
            .where(subjects.c.user_id == user.id, subjects.c.status != "archived", subjects.c.exam_date >= today)
            .order_by(subjects.c.exam_date, subjects.c.id).limit(1)
        ).first()
        return None if row is None else {"subjectId": row.id, "name": row.name, "examDate": row.exam_date.isoformat()}
