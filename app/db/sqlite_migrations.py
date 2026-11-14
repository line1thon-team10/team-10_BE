"""Transactional SQLite DDL without rebuilding parents under cascading FKs."""
from contextlib import contextmanager


@contextmanager
def sqlite_migration_transaction(connection):
    """Own one DDL transaction; never commit caller writes to change PRAGMAs.

    SQLite cannot disable FK checks inside a transaction. Rebuilding a parent
    with checks enabled can cascade-delete children even if the replacement has
    identical keys. Disable only for this explicit transaction, validate all FKs
    before committing, and restore the original setting on either exit path.
    """
    raw = connection.connection.driver_connection
    if raw.in_transaction:
        raise RuntimeError('SQLite migration 전에 호출자의 쓰기를 commit/rollback해 주세요. 임의 commit 없이 중단합니다.')
    if connection.in_transaction():
        # A read-only SQLAlchemy autobegin has no SQLite transaction to preserve.
        connection.rollback()
    enabled = raw.execute('PRAGMA foreign_keys').fetchone()[0]
    raw.execute('PRAGMA foreign_keys=OFF')
    try:
        with connection.begin():
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            yield
            if connection.exec_driver_sql('PRAGMA foreign_key_check').first() is not None:
                raise RuntimeError('외래 키 불변 조건 위반: SQLite migration 전체를 되돌립니다.')
    finally:
        raw.execute(f'PRAGMA foreign_keys={int(enabled)}')
