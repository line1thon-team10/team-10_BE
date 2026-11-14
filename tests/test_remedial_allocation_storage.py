from alembic import command
from sqlalchemy import inspect, select
from app.db import schema as t
from app.db.migrations import migration_config
from tests.test_remedial_storage import test_unique_obligation_links_and_owner_reset as _seed_linked_obligations


def test_allocation_snapshot_migration_preserves_legacy_links(migration_connection):
    conn=migration_connection;config=migration_config(conn)
    command.upgrade(config,'0011_contract_invariants')
    _seed_linked_obligations(conn)
    block=conn.scalar(select(t.remedial_obligation_blocks.c.block_id));conn.commit()
    command.upgrade(config,'0012_remedial_allocation')
    assert conn.scalar(select(t.remedial_obligation_blocks.c.allocation_snapshot).where(t.remedial_obligation_blocks.c.block_id==block)) is None
    snapshot={'schemaVersion':'remedial-allocation-v1','parts':[]}
    conn.execute(t.remedial_obligation_blocks.update().values(allocation_snapshot=snapshot));conn.commit()
    command.downgrade(config,'0011_contract_invariants')
    assert conn.scalar(select(t.remedial_obligation_blocks.c.block_id))==block
    assert 'allocation_snapshot' not in {c['name'] for c in inspect(conn).get_columns('remedial_obligation_blocks')}
    conn.commit();command.upgrade(config,'0012_remedial_allocation')
    assert conn.scalar(select(t.remedial_obligation_blocks.c.block_id))==block
    assert conn.scalar(select(t.remedial_obligation_blocks.c.allocation_snapshot)) is None
