from sqlalchemy import Connection

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.domain.user import User
from app.repositories.users import UserRepository
from app.seed.demo_data import DEFAULT_SEED
from app.seed.loader import load_seed


class SessionService:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.users = UserRepository(connection)

    def create(self, *, demo_mode: bool) -> str:
        """사용자와 데모 데이터를 같은 요청 트랜잭션에 넣는다."""
        user = User.anonymous(now=now_utc())
        self.users.add(user)
        if demo_mode:
            try:
                load_seed(self.connection, user.id, DEFAULT_SEED)
            except ApiError as error:
                # 시드 로더의 내부 파일 경로·구성 오류를 공개 응답에 노출하지 않는다.
                raise ApiError("SERVER_ERROR", "세션을 준비하지 못했어요. 잠시 후 다시 시도해 주세요", 500) from error
        return user.id
