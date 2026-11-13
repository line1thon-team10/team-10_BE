from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.db.migrations import migration_config


def test_focus_indexes_protect_active_and_open_rows(api_client, demo_user, domain_connection):
    conn = domain_connection
    original = dict(conn.execute(select(t.study_sessions)).mappings().one())
    segment = dict(conn.execute(select(t.study_session_segments)).mappings().one())
    conn.commit()
    for status in ("running", "paused"):
        with pytest.raises(IntegrityError):
            conn.execute(t.study_sessions.insert().values(**{**original,"id":str(uuid4()),"status":status}))
        conn.rollback()
    with pytest.raises(IntegrityError):
        conn.execute(t.study_session_segments.insert().values(**{**segment,"id":str(uuid4()),"sort_order":2}))
    conn.rollback()
    # 종료된 세션, 닫힌 구간은 여러 개 보존할 수 있다.
    conn.execute(t.study_sessions.insert().values(**{**original,"id":str(uuid4()),"status":"ended"}))
    conn.execute(t.study_session_segments.insert().values(**{**segment,"id":str(uuid4()),"sort_order":2,
                                                          "ended_at":segment["started_at"]}))
    conn.commit()
    assert compare_metadata(MigrationContext.configure(conn), t.metadata) == []


def test_upgrade_backfills_without_losing_sessions_and_downgrade_retains_rows(api_client,demo_user,domain_connection):
    from app.domain.focus import quote_for
    conn = domain_connection
    sid = conn.scalar(select(t.study_sessions.c.id))
    conn.commit()
    config = migration_config(conn)
    command.downgrade(config,"0003_demo_day_requests")
    conn.commit()
    command.upgrade(config,"head")
    row = conn.execute(select(t.study_sessions)).mappings().one()
    assert row["id"] == sid and row["quote"] == quote_for(sid) and row["expected_seconds"] == 1500
    assert conn.execute(select(t.study_session_segments)).mappings().one()["session_id"] == sid


@pytest.mark.parametrize("duplicate", ["sessions","segments"])
def test_duplicate_legacy_data_aborts_before_any_ddl(api_client,demo_user,domain_connection,duplicate):
    conn = domain_connection
    config = migration_config(conn)
    command.downgrade(config,"0003_demo_day_requests")
    conn.commit()
    from sqlalchemy import MetaData, Table
    table = Table("study_sessions" if duplicate=="sessions" else "study_session_segments", MetaData(), autoload_with=conn)
    row = dict(conn.execute(select(table)).mappings().one())
    row["id"] = uuid4() if conn.dialect.name == "postgresql" else uuid4().hex
    if duplicate == "segments":
        row["sort_order"] = 2
    conn.execute(table.insert().values(**row))
    conn.commit()
    with pytest.raises(RuntimeError, match="중복"):
        command.upgrade(config,"head")
    conn.rollback()
    assert "quote" not in {c["name"] for c in inspect(conn).get_columns("study_sessions")}
    assert len(conn.execute(select(table)).all()) == 2
