"""Migration tests use a temporary SQLite DB or an isolated PostgreSQL schema."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event

DATABASES = ["sqlite"]
if os.getenv("TEST_POSTGRES_URL"):
    DATABASES.append("postgresql")


@pytest.fixture(params=DATABASES)
def migration_connection(request, tmp_path):
    postgres = request.param == "postgresql"
    url = os.environ["TEST_POSTGRES_URL"] if postgres else f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url)
    if not postgres:
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    schema = "test_" + uuid4().hex
    try:
        with engine.connect() as connection:
            if postgres:
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                connection.exec_driver_sql(f'SET search_path TO "{schema}"')
                connection.commit()
            try:
                yield connection
            finally:
                connection.rollback()
                if postgres:
                    connection.exec_driver_sql('SET search_path TO public')
                    connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
                    connection.commit()
    finally:
        engine.dispose()


@pytest.fixture
def domain_connection(migration_connection):
    from alembic import command
    from app.db.migrations import migration_config

    command.upgrade(migration_config(migration_connection), "head")
    migration_connection.commit()
    return migration_connection


@pytest.fixture
def api_client(domain_connection, monkeypatch):
    """실제 lifespan/get_conn을 사용해 요청별 commit/rollback도 검증한다."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from app import main
    from app.db import session

    conn = domain_connection
    if conn.dialect.name == "postgresql":
        schema = conn.scalar(text("select current_schema()"))
        connect_args = {"options": f"-csearch_path={schema}"}
    else:
        connect_args = {"check_same_thread": False}
    conn.commit()
    engine = create_engine(conn.engine.url, connect_args=connect_args)
    if conn.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(session, "engine", engine)
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield client
    finally:
        engine.dispose()
