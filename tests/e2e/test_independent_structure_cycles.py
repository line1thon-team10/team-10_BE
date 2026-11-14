"""Three-way reorder, active/retired isolation and mid-write rollback."""
from sqlalchemy import event, select
from app.db import schema as t
from app.db import session
from app.core.clock import now_utc
from tests.test_units_contract import unit_case, add_unit, add_concept
from tests.test_material_structure_e2e import body_for, put


def snapshot(conn):
    return {table.name: conn.execute(select(table).order_by(table.c.id)).all()
            for table in (t.units, t.concepts, t.materials)}


def test_cycle_failure_after_first_reactivation_rolls_back_then_retries(api_client, domain_connection, unit_case):
    conn, case = domain_connection, unit_case
    ids = [add_unit(conn, case, no=i, start=10*i-9, end=10*i, order=i, mastery_score=20*i)
           for i in (1, 2, 3)]
    concepts = [add_concept(conn, ids[0], start=i, end=i, order=i) for i in (1, 2, 3)]
    retired = add_unit(conn, case, retired_at=now_utc(), mastery_score=91)
    foreign = add_unit(conn, case, material='other_material', mastery_score=99)
    conn.commit()
    before = snapshot(conn)
    conn.commit()
    body = body_for(api_client, case)
    for item, number in zip(body['units'], (2, 3, 1)):
        item['no'] = number
    body['units'][0]['concepts'] = body['units'][0]['concepts'][1:] + body['units'][0]['concepts'][:1]
    writes = []
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE units SET') and 'mastery_score=' in statement:
            writes.append(statement)
            if len(writes) == 2:
                raise RuntimeError('after first identity restored, before second')
    event.listen(session.engine, 'before_cursor_execute', fail)
    try:
        response = put(api_client, case, body)
    finally:
        event.remove(session.engine, 'before_cursor_execute', fail)
    assert response.status_code == 500, response.text
    assert len(writes) == 2
    assert snapshot(conn) == before
    conn.commit()
    response = put(api_client, case, body)
    assert response.status_code == 200, response.text
    units = response.json()['data']['structure']['units']
    assert [row['id'] for row in units] == [ids[2], ids[0], ids[1]]
    assert [row['id'] for row in units[1]['concepts']] == concepts[1:] + concepts[:1]
    after = snapshot(conn)
    for identifier in (retired, foreign):
        assert [row for row in after['units'] if row.id == identifier] == [row for row in before['units'] if row.id == identifier]
    assert set(conn.scalars(select(t.units.c.id))) == set(ids + [retired, foreign])
    assert list(conn.scalars(select(t.units.c.mastery_score).where(t.units.c.id.in_(ids)).order_by(t.units.c.start_page))) == [20, 40, 60]
