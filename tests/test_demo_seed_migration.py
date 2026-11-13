from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from alembic import command
from sqlalchemy import select

from app.db.migrations import migration_config
from app.db.schema import users
from app.repositories.users import UserRepository
from app.seed.loader import load_seed
from migrations.baselines.schema_0001 import users as legacy_users


def test_legacy_same_date_is_not_guessed_and_downgrade_keeps_user(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "0001_baseline")
    user_id = str(uuid4())
    now = datetime.now(timezone.utc)
    conn.execute(legacy_users.insert().values(id=user_id, demo_date=date(2026, 11, 9), created_at=now, updated_at=now))
    conn.commit()
    command.upgrade(config, "0002_demo_seed")
    assert conn.scalar(select(users.c.demo_seed)) is None
    assert conn.scalar(select(users.c.demo_date)) == date(2026, 11, 9)
    conn.commit()
    command.downgrade(config, "0001_baseline")
    assert conn.scalar(select(legacy_users.c.id)) == user_id
    conn.commit()
    command.upgrade(config, "head")
    assert UserRepository(conn).get(UUID(user_id)).demo_seed is None


def test_seed_identity_persists_even_when_dates_match(domain_connection):
    from app.domain.user import User
    conn = domain_connection
    user = User.anonymous(now=datetime.now(timezone.utc))
    UserRepository(conn).add(user)
    for seed in ("day9", "overdue", "empty"):
        load_seed(conn, user.id, seed)
        conn.commit()
        assert UserRepository(conn).get(UUID(user.id)).demo_seed == seed
        conn.commit()
