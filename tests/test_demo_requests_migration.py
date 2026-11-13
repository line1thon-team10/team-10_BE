from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.clock import now_utc
from app.db.migrations import migration_config
from app.db.schema import demo_day_requests, users
from app.domain.user import User
from app.repositories.users import UserRepository


def test_receipts_are_user_scoped_unique_and_keep_json(domain_connection):
    conn=domain_connection
    repo=UserRepository(conn)
    first,second=User.anonymous(now=now_utc()),User.anonymous(now=now_utc())
    repo.add(first);repo.add(second)
    key=str(uuid4())
    for user in (first,second):
        conn.execute(demo_day_requests.insert().values(user_id=user.id,idempotency_key=key,request_hash="hash",
                     response={"seed":"overdue","firstExam":None},created_at=now_utc()))
    conn.commit()
    with pytest.raises(IntegrityError):
        conn.execute(demo_day_requests.insert().values(user_id=first.id,idempotency_key=key,request_hash="hash",
                     response={},created_at=now_utc()))
    conn.rollback()
    assert len(conn.execute(select(demo_day_requests)).all())==2
    assert conn.scalar(select(demo_day_requests.c.response))["firstExam"] is None
    conn.commit()
    command.downgrade(migration_config(conn),"0002_demo_seed")
    assert set(conn.scalars(select(users.c.id)))=={first.id,second.id}
    conn.commit()
    command.upgrade(migration_config(conn),"head")
    assert conn.execute(select(demo_day_requests)).all()==[]
