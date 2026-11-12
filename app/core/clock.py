"""'오늘' 계산 (NF-08). 데모 사용자는 users.demo_date, 아니면 사용자 시간대의 실제 날짜예요.
프론트는 브라우저 시계를 쓰지 않고 서버가 준 날짜만 써요."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


def today_for(user: dict) -> date:
    if user.get("demo_date"):
        return user["demo_date"]
    return datetime.now(ZoneInfo(user.get("timezone") or "Asia/Seoul")).date()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
