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
