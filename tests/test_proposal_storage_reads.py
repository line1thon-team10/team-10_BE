"""A read must not hydrate different statements from different request states."""
import pytest
from sqlalchemy import delete, event

from app.db import schema as t
from app.repositories.proposals import ProposalRepository
from tests.test_proposal_storage import NOW, candidate, owner


@pytest.mark.parametrize("mutation", ["same", "sibling", "delete"])
def test_ready_snapshot_is_consistent_during_a_concurrent_decision(domain_connection, owner, mutation):
    conn = domain_connection
    if conn.dialect.name != "postgresql":
        pytest.skip("Separate READ COMMITTED statement snapshots require PostgreSQL")
    first = candidate(conn, owner)
    second = candidate(conn, owner, request_id=first["request_id"]) if mutation == "sibling" else first
    schema = conn.exec_driver_sql("select current_schema()").scalar()
    conn.commit()
    fired = False

    def commit_after_snapshot(connection, cursor, statement, params, context, many):
        nonlocal fired
        if fired or not statement.startswith("SELECT") or "plan_proposal_states" not in statement:
            return
        fired = True
        with conn.engine.connect() as writer:
            writer.exec_driver_sql(f'SET search_path TO "{schema}"')
            if mutation == "delete":
                writer.execute(delete(t.plan_proposals).where(t.plan_proposals.c.id == first["proposal_id"]))
            else:
                ProposalRepository(writer).mark_applied(owner, second["proposal_id"], expected_revision=0,
                    response={"winner": second["proposal_id"]}, now=NOW)
            writer.commit()

    event.listen(conn, "after_cursor_execute", commit_after_snapshot)
    try:
        snapshot = ProposalRepository(conn).ready(owner, kind="initial")
    finally:
        event.remove(conn, "after_cursor_execute", commit_after_snapshot)
    assert fired and snapshot
    assert all(row["status"] == row["request_status"] == "ready"
               and row["accepted_proposal_id"] is None for row in snapshot)
    # The next statement sees the concurrent commit; the first snapshot is not
    # authorization to apply. SYS-05 must always recheck under the user lock.
    assert ProposalRepository(conn).ready(owner, kind="initial") == []
