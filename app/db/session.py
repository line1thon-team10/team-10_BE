"""DB 연결. 요청마다 트랜잭션 하나를 열고, 끝나면 커밋(에러면 롤백)해요."""
from collections.abc import Iterator

from sqlalchemy import Connection, create_engine, event

from app.config import DATABASE_URL
from app.db.schema import metadata

_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_fk_on(dbapi_conn, _):  # SQLite는 외래키 검사를 직접 켜야 해요
        dbapi_conn.execute("PRAGMA foreign_keys=ON")


def init_db() -> None:
    """테이블이 없으면 만들어요. 마이그레이션 도구는 아직 없어요 (해커톤 기간)."""
    metadata.create_all(engine)


def get_conn() -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn
