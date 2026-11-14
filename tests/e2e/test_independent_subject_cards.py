"""Independent card semantics for immutable unit IDs and user-owned drafts."""
from uuid import uuid4

from sqlalchemy import select, update

from app.core.clock import now_utc
from app.db import schema as t
from tests.e2e.test_subject_flow import empty_account


def test_toc_uses_material_display_number_not_internal_code(api_client, empty_account, domain_connection):
    conn = domain_connection
    response = api_client.post('/api/subjects', headers=empty_account, json={'name': 'Owned subject'})
    assert response.status_code == 200, response.text
    subject = response.json()['data']['subjectId']
    material, unit, now = str(uuid4()), str(uuid4()), now_utc()
    conn.execute(t.materials.insert().values(id=material, subject_id=subject, original_name='lecture.pdf',
        source_format='pdf', sha256='same-file', page_count=10, analysis_version='v1', created_at=now, updated_at=now))
    conn.execute(t.units.insert().values(id=unit, material_id=material, code='immutable-unit-42',
        title='Display number seven', sort_order=7, start_page=1, end_page=10, created_at=now, updated_at=now))
    conn.commit()
    response = api_client.get('/api/subjects', headers=empty_account)
    assert response.status_code == 200, response.text
    toc = response.json()['data']['subjects'][0]['materials'][0]['toc'][0]
    assert toc['unitId'] == unit and toc['no'] == 7


def test_archived_and_removed_cards_preserve_history_without_retired_units(api_client, empty_account, domain_connection):
    conn = domain_connection
    identifiers = [api_client.post('/api/subjects', headers=empty_account, json={'name': name}).json()['data']['subjectId']
                   for name in ('Removed', 'Archived')]
    now = now_utc()
    for index, subject in enumerate(identifiers):
        conn.execute(update(t.subjects).where(t.subjects.c.id == subject).values(status='archived',
            archive_reason='user_removed' if index == 0 else 'exam_finished'))
        material = str(uuid4())
        conn.execute(t.materials.insert().values(id=material, subject_id=subject, original_name='history.pdf',
            source_format='pdf', sha256=material, page_count=10, analysis_version='v1', created_at=now, updated_at=now))
        for retired in (False, True):
            identifier = str(uuid4())
            conn.execute(t.units.insert().values(id=identifier, material_id=material, code='unit_1', title='History',
                sort_order=1, start_page=1, end_page=10, retired_at=now if retired else None,
                created_at=now, updated_at=now))
    tables = (t.subjects, t.materials, t.units, t.plans, t.study_tasks)
    before = {table.name: conn.execute(select(table).order_by(table.c.id)).all() for table in tables}
    conn.commit()
    response = api_client.get('/api/subjects', headers=empty_account)
    assert response.status_code == 200, response.text
    cards = {card['id']: card for card in response.json()['data']['subjects']}
    assert cards[identifiers[0]]['removed'] is True and cards[identifiers[1]]['removed'] is False
    assert all(card['archived'] is True and card['status'] == 'archived' for card in cards.values())
    assert all(len(card['materials'][0]['toc']) == 1 for card in cards.values())
    assert {table.name: conn.execute(select(table).order_by(table.c.id)).all() for table in tables} == before


def test_failed_draft_insert_leaves_no_partial_card_or_plan(api_client, empty_account, domain_connection):
    from sqlalchemy import event
    from app.db.session import engine

    def fail(_conn, _cursor, _statement, _parameters, context, _many):
        if context.isinsert and getattr(context.compiled.statement.table, 'name', None) == 'subjects':
            raise RuntimeError('Independent draft insertion rollback')

    event.listen(engine, 'after_cursor_execute', fail)
    try:
        response = api_client.post('/api/subjects', headers=empty_account, json={'name': 'Retry subject'})
    finally:
        event.remove(engine, 'after_cursor_execute', fail)
    assert response.status_code == 500
    assert api_client.get('/api/subjects', headers=empty_account).json()['data']['subjects'] == []
    assert domain_connection.execute(select(t.subjects)).first() is None
    assert domain_connection.execute(select(t.plans)).first() is None
    domain_connection.commit()
    retried = api_client.post('/api/subjects', headers=empty_account, json={'name': 'Retry subject'})
    assert retried.status_code == 200
    assert len(api_client.get('/api/subjects', headers=empty_account).json()['data']['subjects']) == 1
