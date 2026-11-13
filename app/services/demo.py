from uuid import UUID
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import Connection

from app.core.clock import today_for
from app.core.responses import ApiError
from app.repositories.demo import DemoBusyError, DemoRepository
from app.seed.demo_data import SEEDS
from app.seed.loader import load_seed
from app.services.planner import Planner

NEXT_DAY_HASH = sha256(b"POST /api/demo/next-day\n{}").hexdigest()


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

    def next_day(self, key: UUID | None = None) -> dict:
        user = self._lock_user()
        if key is not None:
            saved = self.repo.completed_request(self.user_id, key)
            if saved is not None:
                if saved["request_hash"] != NEXT_DAY_HASH:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "다른 요청에 사용한 재시도 키예요", 409)
                return saved["response"]
        if not self.repo.has_plan(self.user_id):
            raise ApiError("NO_PLAN", "계획을 먼저 만들어 주세요", 400)
        if user.demo_date is None:
            raise ApiError("BAD_DATE", "데모 날짜가 없는 사용자예요", 400)
        last = self.repo.last_exam(self.user_id)
        if last is None or user.demo_date >= last:
            raise ApiError("DEMO_END", "마지막 시험일까지만 이동할 수 있어요", 400)
        tomorrow = user.demo_date + timedelta(days=1)
        try:
            judgment = Planner(self.connection, user.to_mapping()).advance_day()
        except ApiError as error:
            if error.code == "ACTIVE_STUDY_SESSION":
                raise ApiError("SESSION_RUNNING", "집중 모드를 먼저 끝내 주세요", 409) from error
            raise
        self.repo.archive_finished(self.user_id, tomorrow)
        response = self.state(judgment)
        if key is not None:
            self.repo.save_request(self.user_id, key, NEXT_DAY_HASH, response)
        return response
