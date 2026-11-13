from datetime import timezone
from uuid import UUID

from sqlalchemy import Connection, select

from app.db.schema import users
from app.domain.user import User


class UserRepository:
    def __init__(self, connection: Connection):
        self.connection = connection

    def add(self, user: User) -> None:
        self.connection.execute(users.insert().values(**user.to_mapping()))

    def get(self, user_id: UUID) -> User | None:
        row = self.connection.execute(
            select(users).where(users.c.id == str(user_id))
        ).mappings().first()
        if row is None:
            return None
        values = dict(row)
        # SQLite DateTime은 timezone 정보를 보존하지 않는다. 저장 규약은 UTC다.
        for field in ("created_at", "updated_at"):
            if values[field].tzinfo is None:
                values[field] = values[field].replace(tzinfo=timezone.utc)
        return User(**values)
