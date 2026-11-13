from uuid import UUID

from sqlalchemy import Connection

from app.core.clock import today_for
from app.core.responses import ApiError
from app.repositories.demo import DemoBusyError, DemoRepository
from app.seed.demo_data import SEEDS
from app.seed.loader import load_seed


class DemoService:
    def __init__(self, connection: Connection, user_id: str):
        self.connection = connection
        self.user_id = UUID(user_id)
        self.repo = DemoRepository(connection)

    def _lock_user(self):
        try:
            user = self.repo.lock_user(self.user_id)
        except DemoBusyError:
            raise ApiError("REQUEST_IN_PROGRESS", "이 사용자의 변경 작업이 진행 중이에요. 잠시 후 다시 시도해 주세요", 409) from None
        if user is None:
            raise ApiError("UNKNOWN_SESSION", "알 수 없는 익명 ID예요. 새로 발급받아 주세요", 401)
        return user

    def state(self, judgment: str | None = None) -> dict:
        user = self.repo.users.get(self.user_id)
        if user is None:
            raise ApiError("UNKNOWN_SESSION", "알 수 없는 익명 ID예요. 새로 발급받아 주세요", 401)
        return {"today": today_for(user.to_mapping()).isoformat(), "firstExam": self.repo.first_exam(user),
                "seed": user.demo_seed, "seeds": SEEDS, "hasPlan": self.repo.has_plan(self.user_id),
                "judgment": judgment}

    def seed(self, name: str) -> dict:
        if name not in SEEDS:
            raise ApiError("BAD_SEED", "없는 시드예요", 400)
        self._lock_user()
        try:
            load_seed(self.connection, str(self.user_id), name)
        except ApiError as error:
            raise ApiError("SERVER_ERROR", "데모를 준비하지 못했어요. 잠시 후 다시 시도해 주세요", 500) from error
        return self.state()
