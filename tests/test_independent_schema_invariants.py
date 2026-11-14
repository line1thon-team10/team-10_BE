"""Independent populated DB and transactional failure oracles for SYS03."""
import json
from datetime import datetime, timezone
from uuid import uuid4

from alembic import command
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import event, inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.db import schema as t
from app.db.migrations import migration_config
from tests.test_quiz_storage_migration import seed_legacy

OLD = '0010_remedial_obligations'
NEW = '0011_contract_invariants'


def records(conn):
    return {name: conn.execute(select(table).order_by(*table.primary_key)).all()
            for name, table in t.metadata.tables.items()}


def shape(conn):
    inspector = inspect(conn)
    return json.dumps({name: (inspector.get_check_constraints(name), inspector.get_indexes(name),
                   inspector.get_foreign_keys(name)) for name in inspector.get_table_names()},
                   default=str, sort_keys=True)


def multiowner(conn):
    config = migration_config(conn)
    command.upgrade(config, '0005_onboarding_state')
    owners = [seed_legacy(conn) for _ in range(3)]
    command.upgrade(config, OLD)
    # Backfilled answer/head/job state is a dependent FK graph, not merely empty tables.
    assert conn.scalar(select(t.quiz_answers.c.id))
    assert conn.scalar(select(t.quiz_answer_heads.c.answer_id))
    assert conn.scalar(select(t.quiz_grading_jobs.c.id))
    conn.commit()
    return config, owners


def test_three_owner_fk_graph_and_roundtrip(migration_connection):
    conn = migration_connection
    config, owners = multiowner(conn)
    before = records(conn)
    conn.commit()
    for direction, target in [(command.upgrade, NEW), (command.downgrade, OLD), (command.upgrade, NEW)]:
        direction(config, target)
        assert records(conn) == before
        assert set(conn.scalars(select(t.subjects.c.user_id))) == set(owners)
        assert MigrationContext.configure(conn).get_current_revision() == target
        if conn.dialect.name == 'sqlite':
            assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
            assert conn.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        conn.commit()
    # FK enforcement really remains active after all rebuilds on the same connection.
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(t.quiz_items).values(question_id=str(uuid4())))


@pytest.mark.parametrize('failure', ['preflight', 'late_ddl'])
def test_failure_restores_all_rows_constraints_and_revision(migration_connection, failure):
    conn = migration_connection
    config, _ = multiowner(conn)
    if failure == 'preflight':
        conn.execute(update(t.units).where(t.units.c.id == conn.scalar(select(t.units.c.id))).values(start_page=8, end_page=7))
        conn.commit()
    before, schema = records(conn), shape(conn)
    conn.commit()
    triggered = []
    def inject(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('CREATE UNIQUE INDEX') and 'uq_study_tasks_active_block' in statement:
            triggered.append(statement)
            raise RuntimeError('independent late index failure')
    if failure == 'late_ddl':
        event.listen(conn, 'before_cursor_execute', inject)
    try:
        with pytest.raises(RuntimeError):
            command.upgrade(config, NEW)
    finally:
        if failure == 'late_ddl':
            event.remove(conn, 'before_cursor_execute', inject)
    conn.rollback()
    assert bool(triggered) == (failure == 'late_ddl')
    assert records(conn) == before
    assert shape(conn) == schema
    assert MigrationContext.configure(conn).get_current_revision() == OLD
    if conn.dialect.name == 'sqlite':
        assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
    conn.commit()
    if failure == 'late_ddl':
        command.upgrade(config, NEW)
        assert records(conn) == before


def test_partial_uniqueness_applies_to_reactivation_not_history(migration_connection):
    conn = migration_connection
    config, _ = multiowner(conn)
    command.upgrade(config, NEW)
    for table, historical in [(t.units, {'retired_at': datetime.now(timezone.utc)}),
                              (t.concepts, {'retired_at': datetime.now(timezone.utc)}),
                              (t.study_tasks, {'status': 'completed'})]:
        query = select(table)
        if table is t.study_tasks:
            query = query.where(table.c.status.in_(('planned', 'partial')))
        original = dict(conn.execute(query).mappings().first())
        old_id = str(uuid4())
        conn.execute(table.insert().values(**{**original, 'id': old_id, **historical}))
        activation = {'status': 'partial'} if table is t.study_tasks else {'retired_at': None}
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(update(table).where(table.c.id == old_id).values(**activation))
        conn.execute(update(table).where(table.c.id == original['id']).values(**historical))
        conn.execute(update(table).where(table.c.id == old_id).values(**activation))
        assert conn.scalar(select(table.c.id).where(table.c.id == original['id'])) == original['id']
    conn.commit()


def test_sqlite_disabled_foreign_keys_restored_after_success_and_failure(migration_connection):
    conn = migration_connection
    if conn.dialect.name != 'sqlite':
        pytest.skip('SQLite connection setting')
    config, _ = multiowner(conn)
    raw = conn.connection.driver_connection
    raw.execute('PRAGMA foreign_keys=OFF')
    command.upgrade(config, NEW)
    assert raw.execute('PRAGMA foreign_keys').fetchone()[0] == 0
    command.downgrade(config, OLD)
    conn.execute(update(t.units).values(start_page=5, end_page=1))
    conn.commit()
    with pytest.raises(RuntimeError):
        command.upgrade(config, NEW)
    assert raw.execute('PRAGMA foreign_keys').fetchone()[0] == 0
    raw.execute('PRAGMA foreign_keys=ON')
