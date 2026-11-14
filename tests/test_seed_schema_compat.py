"""Historical schema seeding must not require later request sidecars."""
from datetime import datetime, timezone
import pytest
from alembic import command
from sqlalchemy import select
from app.db import schema as t
from app.db.migrations import migration_config
from app.domain.user import User
from app.repositories.users import UserRepository
from app.seed.loader import load_seed


@pytest.mark.parametrize('revision', ['0005_onboarding_state','0006_quiz_answer_storage','0008_grading_execution_mode','head'])
def test_seed_empty_is_supported_before_request_sidecars(migration_connection,revision):
    conn=migration_connection
    command.upgrade(migration_config(conn),revision)
    now=datetime(2026,11,14,tzinfo=timezone.utc)
    own,other=User.anonymous(now=now),User.anonymous(now=now)
    repo=UserRepository(conn);repo.add(own);repo.add(other)
    before=dict(conn.execute(select(t.users).where(t.users.c.id==other.id)).mappings().one())
    load_seed(conn,own.id,'empty')
    assert conn.scalar(select(t.users.c.demo_seed).where(t.users.c.id==own.id))=='empty'
    assert dict(conn.execute(select(t.users).where(t.users.c.id==other.id)).mappings().one())==before
