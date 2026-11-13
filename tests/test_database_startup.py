import asyncio

from alembic import command
import pytest
from sqlalchemy import inspect, text

from app.db.migrations import migration_config, require_current_schema


def test_uninitialized_startup_is_read_only(migration_connection):
    conn = migration_connection
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        require_current_schema(conn)
    assert inspect(conn).get_table_names() == []


@pytest.mark.parametrize("revision", ["unknown_revision", None, "multiple"])
def test_invalid_revision_blocks_startup_without_repair(migration_connection, revision):
    conn = migration_connection
    command.upgrade(migration_config(conn), "head")
    if revision == "multiple":
        conn.execute(text("INSERT INTO alembic_version VALUES ('other_revision')"))
    elif revision:
        conn.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": revision})
    else:
        conn.execute(text("DELETE FROM alembic_version"))
    conn.commit()
    before = conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).all()
    with pytest.raises(RuntimeError, match="migration"):
        require_current_schema(conn)
    assert conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).all() == before


def test_application_lifespan_requires_migration(monkeypatch, migration_connection):
    from app import main

    conn = migration_connection
    monkeypatch.setattr(main, "check_db_revision", lambda: require_current_schema(conn))

    async def start():
        async with main.lifespan(main.app):
            return "started"

    with pytest.raises(RuntimeError, match="migration"):
        asyncio.run(start())
    conn.rollback()
    command.upgrade(migration_config(conn), "head")
    assert asyncio.run(start()) == "started"
