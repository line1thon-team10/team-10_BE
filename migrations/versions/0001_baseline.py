"""Freeze the pre-migration schema without changing product domains."""
from alembic import op
from sqlalchemy import MetaData, inspect

from migrations.baselines.schema_0001 import metadata

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def snapshot():
    # SQLAlchemy의 ALTER FK DDL은 constraint 객체에 실행 상태를 남길 수 있어요.
    # 같은 프로세스에서 SQLite/PG 또는 upgrade/downgrade를 반복해도 원본은 보존해요.
    copy = MetaData()
    for table in metadata.tables.values():
        table.to_metadata(copy)
    return copy


def upgrade():
    if not op.get_context().as_sql:
        inspector = inspect(op.get_bind())
        tables = set(inspector.get_table_names()) - {"alembic_version"}
        if tables or inspector.get_view_names():
            raise RuntimeError(
                "기존 DB에는 baseline을 덮어쓰지 않습니다. 백업 후 "
                "python -m app.db.migrations adopt-baseline으로 구조를 검증하세요."
            )
    snapshot().create_all(op.get_bind(), checkfirst=False)


def downgrade():
    snapshot().drop_all(op.get_bind(), checkfirst=False)
