"""Explicit migration execution; importing the application never runs DDL."""
from alembic import context
from sqlalchemy import create_engine, event, pool

from app.config import DATABASE_URL
from app.db.schema import metadata

config = context.config


def configure(connection):
    context.configure(
        connection=connection,
        target_metadata=metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=connection.dialect.name == "sqlite",
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=DATABASE_URL,
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    configure(config.attributes["connection"])
else:
    # URL을 ConfigParser에 넣지 않아 비밀번호의 '%'도 보존해요.
    engine = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    try:
        with engine.connect() as connection:
            configure(connection)
    finally:
        engine.dispose()
