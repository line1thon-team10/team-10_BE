"""Enforce current identities and scalar invariants without repairing old rows."""
import json
from pathlib import Path
from collections import defaultdict
from alembic import context, op
import sqlalchemy as sa

revision = '0011_contract_invariants'
down_revision = '0010_remedial_obligations'
branch_labels = None
depends_on = None

# Immutable migration input; later application schema changes cannot rewrite it.
CONTRACT = json.loads((Path(__file__).parents[1] / 'baselines' / 'contract_invariants_0011.json').read_text())


def _preflight(conn):
    for check in CONTRACT['checks']:
        if conn.execute(sa.text(f"SELECT 1 FROM {check['table']} WHERE NOT ({check['sql']}) LIMIT 1")).first():
            raise RuntimeError(f"{check['name']}: 기존 불량 행을 삭제하거나 고치지 않고 migration을 중단합니다.")
    for index in CONTRACT['indexes']:
        columns = ', '.join(index['columns'])
        nonnull = ' AND '.join(f'{column} IS NOT NULL' for column in index['columns'])
        sql = f"SELECT 1 FROM {index['table']} WHERE ({index['where']}) AND {nonnull} GROUP BY {columns} HAVING count(*) > 1 LIMIT 1"
        if conn.execute(sa.text(sql)).first():
            raise RuntimeError(f"{index['name']}: 기존 중복 행을 삭제하거나 고치지 않고 migration을 중단합니다.")


def _checks(create):
    by_table = defaultdict(list)
    for check in CONTRACT['checks']:
        by_table[check['table']].append(check)
    reflected = None
    if not context.is_offline_mode() and op.get_bind().dialect.name == 'sqlite':
        # Reflect the FK graph once, rather than once per parent rebuild.
        reflected = sa.MetaData()
        reflected.reflect(bind=op.get_bind())
    for table, checks in by_table.items():
        copy = reflected.tables[table] if reflected is not None else None
        with op.batch_alter_table(table, copy_from=copy) as batch:
            for check in checks:
                if create:
                    batch.create_check_constraint(check['name'], check['sql'])
                else:
                    batch.drop_constraint(check['name'], type_='check')


def upgrade():
    if not context.is_offline_mode():
        _preflight(op.get_bind())
    _checks(True)
    for index in CONTRACT['indexes']:
        op.create_index(index['name'], index['table'], index['columns'], unique=True,
            postgresql_where=sa.text(index['where']), sqlite_where=sa.text(index['where']))


def downgrade():
    for index in reversed(CONTRACT['indexes']):
        op.drop_index(index['name'], table_name=index['table'])
    _checks(False)
