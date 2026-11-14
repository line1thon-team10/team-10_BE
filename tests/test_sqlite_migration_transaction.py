import pytest
from sqlalchemy import create_engine
from app.db.sqlite_migrations import sqlite_migration_transaction


@pytest.fixture
def connection():
    engine=create_engine('sqlite://')
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        conn.exec_driver_sql('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE child (id INTEGER REFERENCES parent(id) ON DELETE CASCADE)')
        conn.exec_driver_sql('INSERT INTO parent VALUES(1)')
        conn.exec_driver_sql('INSERT INTO child VALUES(1)')
        conn.commit()
        yield conn
    engine.dispose()


def rebuild(conn):
    conn.exec_driver_sql('CREATE TABLE replacement (id INTEGER PRIMARY KEY CHECK(id>0))')
    conn.exec_driver_sql('INSERT INTO replacement SELECT * FROM parent')
    conn.exec_driver_sql('DROP TABLE parent')
    conn.exec_driver_sql('ALTER TABLE replacement RENAME TO parent')


def test_rebuilding_parent_preserves_cascading_children(connection):
    with sqlite_migration_transaction(connection):
        rebuild(connection)
    assert connection.exec_driver_sql('SELECT * FROM child').all()==[(1,)]
    assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar()==1


def test_ddl_failure_rolls_back_rebuild_and_restores_fk_checks(connection):
    with pytest.raises(ValueError):
        with sqlite_migration_transaction(connection):
            rebuild(connection)
            raise ValueError('injected migration failure')
    assert connection.exec_driver_sql('SELECT * FROM child').all()==[(1,)]
    assert connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='parent'").scalar()=='CREATE TABLE parent (id INTEGER PRIMARY KEY)'
    assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar()==1


def test_invalid_foreign_key_rolls_back_all_ddl(connection):
    with pytest.raises(RuntimeError,match='외래 키'):
        with sqlite_migration_transaction(connection):
            rebuild(connection)
            connection.exec_driver_sql('INSERT INTO child VALUES(999)')
    assert connection.exec_driver_sql('SELECT * FROM child').all()==[(1,)]
    assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar()==1


def test_never_commits_uncommitted_caller_writes(connection):
    connection.exec_driver_sql('INSERT INTO parent VALUES(2)')
    with pytest.raises(RuntimeError,match='호출자의 쓰기'):
        with sqlite_migration_transaction(connection):
            pytest.fail('must reject before DDL')
    connection.rollback()
    assert connection.exec_driver_sql('SELECT * FROM parent').all()==[(1,)]
    assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar()==1
