"""Record replay migration can be removed without losing domain rows."""
from datetime import datetime, timezone
from uuid import uuid4

from alembic import command
from sqlalchemy import inspect, select

from app.db import schema as t
from app.db.migrations import migration_config


def test_record_request_migration_downgrade_preserves_users(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, '0008_grading_execution_mode')
    now, user_id = datetime.now(timezone.utc), str(uuid4())
    conn.execute(t.users.insert().values(id=user_id, created_at=now, updated_at=now))
    conn.commit()
    command.upgrade(config, '0009_record_requests')
    conn.execute(t.record_requests.insert().values(user_id=user_id, method='POST',path='/api/records',
        idempotency_key=str(uuid4()), request_hash='a'*64, response={'success':True,'data':{'event':None}},created_at=now))
    conn.commit()
    command.downgrade(config, '0008_grading_execution_mode')
    assert 'record_requests' not in inspect(conn).get_table_names()
    assert conn.scalar(select(t.users.c.id)) == user_id
    conn.commit()
    command.upgrade(config, '0009_record_requests')
    assert conn.execute(select(t.record_requests)).first() is None
    assert conn.scalar(select(t.users.c.id)) == user_id
