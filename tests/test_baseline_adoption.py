from datetime import datetime, timezone
from uuid import uuid4

from alembic import command
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import MetaData, inspect, text

from app.db.migrations import BASELINE, adopt_baseline, migration_config
from migrations.baselines.schema_0001 import metadata, users


def create_legacy_database(connection):
    legacy = MetaData()
    for table in metadata.tables.values():
        table.to_metadata(legacy)
    legacy.create_all(connection)
    user_id = str(uuid4())
    now = datetime.now(timezone.utc)
    connection.execute(users.insert().values(id=user_id, created_at=now, updated_at=now))
    connection.commit()
    return user_id


def test_legacy_adoption_and_repeat_preserve_rows(migration_connection):
    conn = migration_connection
    user_id = create_legacy_database(conn)
    before = conn.execute(users.select()).mappings().all()
    conn.commit()
    with conn.begin():
        assert adopt_baseline(conn) is True
    with conn.begin():
        assert adopt_baseline(conn) is False
    command.upgrade(migration_config(conn), "head")
    assert conn.execute(users.select()).mappings().all() == before
    assert before[0]["id"] == user_id
    assert MigrationContext.configure(conn).get_current_heads() == (BASELINE,)


@pytest.mark.parametrize("change", [
    "missing_table", "extra_table", "extra_column", "missing_index", "missing_fk", "partial_index",
    "wrong_type", "wrong_nullability", "wrong_pk", "wrong_default", "extra_check", "extra_view",
])
def test_schema_drift_is_not_stamped(migration_connection, change):
    conn = migration_connection
    from sqlalchemy import Column, MetaData, String, Table

    # PK/type/null/default/FK도 검증하는지 실제 DDL로 확인해요.
    legacy = MetaData()
    for table in metadata.tables.values():
        table.to_metadata(legacy)
    if change == "wrong_type":
        legacy.tables["users"].c.timezone.type = String(12)
    elif change == "wrong_nullability":
        legacy.tables["users"].c.timezone.nullable = True
    elif change == "wrong_default":
        from sqlalchemy import DefaultClause
        legacy.tables["users"].c.timezone.server_default = DefaultClause("UTC")
    elif change == "wrong_pk":
        # 다른 테이블이 참조하지 않는 로그 테이블에서 PK를 제거해요.
        legacy.remove(legacy.tables["ai_usage_logs"])
        original = metadata.tables["ai_usage_logs"]
        Table("ai_usage_logs", legacy, *[
            Column(column.name, column.type, nullable=column.nullable)
            for column in original.columns
        ])
    elif change == "missing_fk":
        table = legacy.tables["subjects"]
        constraint = next(iter(table.foreign_key_constraints))
        table.constraints.remove(constraint)
    legacy.create_all(conn)
    if change == "missing_table":
        conn.exec_driver_sql("DROP TABLE ai_usage_logs")
    elif change == "extra_table":
        conn.exec_driver_sql("CREATE TABLE unknown_table (id INTEGER)")
    elif change == "extra_column":
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN unexpected TEXT")
    elif change == "missing_index":
        conn.exec_driver_sql("DROP INDEX ix_subjects_1")
    elif change == "partial_index":
        conn.exec_driver_sql("DROP INDEX ix_subjects_1")
        conn.exec_driver_sql("CREATE UNIQUE INDEX ix_subjects_1 ON subjects(user_id,code) WHERE status='active'")
    elif change == "extra_check":
        # SQLite에서는 테이블 재작성 없이 CHECK를 가진 추가 컬럼으로 시험해요.
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN marker INTEGER CHECK (marker > 0)")
    elif change == "extra_view":
        conn.exec_driver_sql("CREATE VIEW users_view AS SELECT id FROM users")
    conn.commit()
    with pytest.raises(RuntimeError):
        with conn.begin():
            adopt_baseline(conn)
    assert MigrationContext.configure(conn).get_current_heads() == ()
    assert "alembic_version" not in inspect(conn).get_table_names()


def test_empty_database_is_not_stamped(migration_connection):
    conn = migration_connection
    with pytest.raises(RuntimeError, match="테이블"):
        with conn.begin():
            adopt_baseline(conn)
    assert inspect(conn).get_table_names() == []


def test_unknown_revision_is_not_replaced(migration_connection):
    conn = migration_connection
    create_legacy_database(conn)
    with conn.begin():
        adopt_baseline(conn)
        conn.execute(text("UPDATE alembic_version SET version_num='unknown_revision'"))
    with pytest.raises(RuntimeError, match="다른 revision"):
        with conn.begin():
            adopt_baseline(conn)
    assert MigrationContext.configure(conn).get_current_heads() == ("unknown_revision",)


def test_caller_rollback_does_not_leave_baseline_version(migration_connection):
    conn = migration_connection
    user_id = create_legacy_database(conn)
    with pytest.raises(RuntimeError, match="abort"):
        with conn.begin():
            adopt_baseline(conn)
            raise RuntimeError("abort")
    assert MigrationContext.configure(conn).get_current_heads() == ()
    assert conn.execute(users.select()).mappings().one()["id"] == user_id
