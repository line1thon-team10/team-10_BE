"""ERD users의 상태. 익명 ID는 이번 계약에서 소지 기반 자격이다."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from uuid import uuid4


@dataclass(frozen=True, repr=False)
class User:
    # repr에 소지 기반 자격인 id가 포함되지 않도록 한다.
    id: str
    timezone: str
    onboarding_status: str
    demo_date: date | None
    last_assessed_date: date | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def anonymous(cls, *, now: datetime) -> "User":
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("User creation time must include a timezone")
        now = now.astimezone(timezone.utc)
        return cls(str(uuid4()), "Asia/Seoul", "draft", None, None, now, now)

    def to_mapping(self) -> dict:
        """기존 서비스의 내부 dict 계약을 유지한다. 공개 응답에 사용하지 않는다."""
        return asdict(self)
