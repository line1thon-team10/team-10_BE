"""Inspect and adopt the legacy schema without rewriting application data."""
import argparse
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect

from migrations.baselines.schema_0001 import metadata as baseline

BASELINE = "0001_baseline"
ROOT = Path(__file__).resolve().parents[2]


def migration_config(connection: Connection | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def validate_baseline(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    if tables != set(baseline.tables) or inspector.get_view_names():
        raise RuntimeError("baseline과 테이블/뷰 구성이 다릅니다. 변경 없이 중단합니다.")

    # Alembic autogenerate만으로 PK/일부 CHECK/부분 인덱스 변화를 판정하지 않아요.
    for table in baseline.tables.values():
        reflected = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(reflected) != set(table.c.keys()):
            raise RuntimeError(f"{table.name}: baseline과 컬럼 구성이 다릅니다.")
        for column in table.columns:
            # autogenerate는 무제한 VARCHAR와 VARCHAR(n) 차이를 생략할 수 있어요.
            expected_type = str(column.type.compile(dialect=connection.dialect))
            actual_type = str(reflected[column.name]["type"].compile(dialect=connection.dialect))
            if expected_type != actual_type:
                raise RuntimeError(f"{table.name}.{column.name}: baseline과 타입/길이가 다릅니다.")
        primary_key = inspector.get_pk_constraint(table.name)["constrained_columns"]
        if primary_key != [column.name for column in table.primary_key.columns]:
            raise RuntimeError(f"{table.name}: baseline과 기본 키가 다릅니다.")
        if inspector.get_check_constraints(table.name):
            raise RuntimeError(f"{table.name}: baseline에 없는 CHECK 제약이 있습니다.")
        for index in inspector.get_indexes(table.name):
            options = index.get("dialect_options", {})
            if options.get(f"{connection.dialect.name}_where") is not None:
                raise RuntimeError(f"{table.name}: baseline에 없는 부분 인덱스가 있습니다.")

    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    differences = compare_metadata(context, baseline)
    if differences:
        raise RuntimeError(
            f"baseline과 컬럼/타입/NULL/default/외래키/인덱스/유일 제약에 "
            f"{len(differences)}건 차이가 있습니다. 변경 없이 중단합니다."
        )


def adopt_baseline(connection: Connection) -> bool:
    """Caller owns the transaction; validation failures never stamp the database."""
    heads = MigrationContext.configure(connection).get_current_heads()
    if heads and heads != (BASELINE,):
        raise RuntimeError("이미 다른 revision으로 관리되는 DB입니다. baseline으로 되돌리지 않습니다.")
    validate_baseline(connection)
    if heads:
        return False
    config = migration_config(connection)
    config.attributes["preserve_transaction"] = True
    command.stamp(config, BASELINE)
    return True


def require_current_schema(connection: Connection) -> None:
    """Read-only startup gate: operators run migrations before starting workers."""
    expected = tuple(ScriptDirectory.from_config(migration_config()).get_heads())
    if len(expected) != 1:
        raise RuntimeError("migration head가 하나여야 합니다. 스택의 revision 의존성을 정리하세요.")
    actual = MigrationContext.configure(connection).get_current_heads()
    if actual != expected:
        raise RuntimeError(
            "DB migration이 필요합니다. 서버 시작 전에 alembic upgrade head를 실행하세요. "
            "기존 create_all DB는 먼저 python -m app.db.migrations adopt-baseline을 실행하세요."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["adopt-baseline"])
    parser.parse_args()
    from app.db.session import engine

    try:
        with engine.begin() as connection:
            changed = adopt_baseline(connection)
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    print("기존 데이터 유지, baseline 등록 완료." if changed else "이미 baseline으로 등록되어 있습니다.")


if __name__ == "__main__":
    main()
