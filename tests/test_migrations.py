from datetime import datetime, timezone
import io
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import inspect, text

from app.db.schema import metadata as current_metadata
from migrations.baselines.schema_0001 import metadata, users

ROOT = Path(__file__).resolve().parents[1]


def migration_config(connection):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def seed_user(connection):
    user_id = str(uuid4())
    now = datetime.now(timezone.utc)
    connection.execute(users.insert().values(id=user_id, created_at=now, updated_at=now))
    connection.commit()
    return user_id


def assert_no_schema_diff(connection, expected):
    context = MigrationContext.configure(connection, opts={"compare_server_default": True})
    assert compare_metadata(context, expected) == []


def test_fresh_upgrade_matches_schema_and_second_upgrade_preserves_data(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "head")
    assert MigrationContext.configure(conn).get_current_heads() == ("0001_baseline",)
    assert set(inspect(conn).get_table_names()) == set(metadata.tables) | {"alembic_version"}
    assert_no_schema_diff(conn, current_metadata)
    user_id = seed_user(conn)
    command.upgrade(config, "head")
    assert conn.execute(users.select()).mappings().one()["id"] == user_id


def test_empty_baseline_can_downgrade_and_reupgrade(migration_connection):
    conn = migration_connection
    config = migration_config(conn)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
    conn.commit()
    command.upgrade(config, "head")
    assert_no_schema_diff(conn, metadata)


def test_upgrade_refuses_unversioned_existing_data(migration_connection):
    conn = migration_connection
    metadata.create_all(conn)
    user_id = seed_user(conn)
    with pytest.raises(RuntimeError, match="adopt-baseline"):
        command.upgrade(migration_config(conn), "head")
    conn.rollback()
    assert conn.execute(users.select()).mappings().one()["id"] == user_id
    assert MigrationContext.configure(conn).get_current_heads() == ()


def test_upgrade_refuses_partial_or_unrelated_database(migration_connection):
    conn = migration_connection
    conn.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
    conn.execute(text("INSERT INTO unrelated (id) VALUES (7)"))
    conn.commit()
    with pytest.raises(RuntimeError, match="기존 DB"):
        command.upgrade(migration_config(conn), "head")
    conn.rollback()
    assert conn.execute(text("SELECT id FROM unrelated")).scalar_one() == 7
    assert "users" not in inspect(conn).get_table_names()


def test_postgres_sql_rendering_does_not_remove_sqlite_cycle_foreign_keys(migration_connection):
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    with patch("app.config.DATABASE_URL", "postgresql+psycopg://test@localhost/test"):
        command.upgrade(config, "head", sql=True)
    assert "ALTER TABLE" in output.getvalue()
    conn = migration_connection
    command.upgrade(migration_config(conn), "head")
    assert_no_schema_diff(conn, metadata)
