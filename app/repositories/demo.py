from uuid import UUID

from sqlalchemy import Connection, func, select, update

from app.core.clock import today_for
from app.core.clock import now_utc
from app.db.schema import demo_day_requests, plans, subjects, users
from app.repositories.users import UserRepository
from app.repositories.locking import UserBusyError, lock_user


DemoBusyError = UserBusyError


class DemoRepository:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.users = UserRepository(connection)

    def lock_user(self, user_id: UUID):
        return lock_user(self.connection, user_id)

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

    def last_exam(self, user_id: UUID):
        return self.connection.scalar(select(func.max(subjects.c.exam_date)).where(
            subjects.c.user_id == str(user_id), subjects.c.status == "active"))

    def archive_finished(self, user_id: UUID, today):
        now = now_utc()
        self.connection.execute(update(subjects).where(
            subjects.c.user_id == str(user_id), subjects.c.status == "active", subjects.c.exam_date < today
        ).values(status="archived", archive_reason="exam_finished", archived_at=now, updated_at=now))

    def completed_request(self, user_id: UUID, key: UUID):
        return self.connection.execute(select(demo_day_requests).where(
            demo_day_requests.c.user_id == str(user_id), demo_day_requests.c.idempotency_key == str(key)
        )).mappings().first()

    def save_request(self, user_id: UUID, key: UUID, request_hash: str, response: dict):
        self.connection.execute(demo_day_requests.insert().values(
            user_id=str(user_id), idempotency_key=str(key), request_hash=request_hash,
            response=response, created_at=now_utc()))
