"""Independent B-03 HTTP views: three-generation lineage and reversals."""
from datetime import timedelta
from sqlalchemy import select, update
from app.db import schema as t
from tests.test_progress_contract import progress_world


def book(client, w, unit):
    response = client.get('/api/books', headers={'X-Anonymous-Id': w.user})
    assert response.status_code == 200, response.text
    return next(u for s in response.json()['data']['subjects'] for u in s['units'] if u['id'] == unit['id'])


def recent(client, w, block):
    response = client.get('/api/today', headers={'X-Anonymous-Id': w.user})
    assert response.status_code == 200, response.text
    return {d['date']: next((b for b in d['blocks'] if b['id'] == block), None)
            for d in response.json()['data']['recentDays']}


def test_three_generation_book_keeps_holes_and_reopens_on_middle_correction(api_client, progress_world):
    w = progress_world
    unit = w.unit(1, 10, mastery_level=None)
    root = w.task((unit, 1, 10), done=[(unit, 1, 3)], status='carried_over', day=w.day - timedelta(days=2))
    mid = w.task((unit, 4, 10), done=[(unit, 4, 7)], status='carried_over', day=w.day - timedelta(days=1))
    tail = w.task((unit, 8, 10), done=[(unit, 8, 10)], status='completed')
    bid = w.conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id == root))
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == mid).values(block_id=bid, origin_task_id=root))
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == tail).values(block_id=bid, origin_task_id=mid))
    w.conn.commit()
    result = book(api_client, w, unit)
    assert [(b['id'], b['state']) for b in result['blocks']] == [(bid, 'done')]
    assert result['blocks'][0]['task']['id'] == tail
    days = recent(api_client, w, bid)
    assert [days[(w.day - timedelta(days=i)).isoformat()]['state'] for i in (2, 1, 0)] == ['doing', 'doing', 'done']
    for end, expected in ((5, 'doing'), (7, 'done')):
        response = api_client.patch(f'/api/tasks/{mid}/record', headers={'X-Anonymous-Id': w.user}, json={'toPage': end})
        assert response.status_code == 200, response.text
        assert book(api_client, w, unit)['blocks'][0]['state'] == expected
        assert recent(api_client, w, bid)[w.day.isoformat()]['state'] == expected
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == mid)) == 'carried_over'


def test_same_pdf_other_unit_record_cannot_complete_block(api_client, progress_world):
    w = progress_world
    unit = w.unit(1, 10, mastery_level=None)
    wrong = w.unit(1, 10, material_id=unit['material_id'], sort_order=unit['sort_order'] + 1)
    task = w.task((unit, 1, 10), done=[(wrong, 1, 10)], status='completed')
    w.conn.commit()
    target = book(api_client, w, unit)
    assert len(target['blocks']) == 1
    assert target['blocks'][0]['state'] == 'planned'
    assert target['blocks'][0]['task']['donePages'] == 0


def test_none_correction_and_retired_history_remain_owned_and_read_only(api_client, progress_world):
    w = progress_world
    unit = w.unit(1, 10, mastery_level=None)
    task = w.task((unit, 1, 10), done=[(unit, 1, 10)], status='completed')
    w.conn.execute(update(t.units).where(t.units.c.id == unit['id']).values(retired_at=w.now))
    w.conn.commit()
    assert book(api_client, w, unit)['blocks'][0]['state'] == 'done'
    response = api_client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': w.user}, json={'completedRanges': []})
    assert response.status_code == 200, response.text
    state = book(api_client, w, unit)
    assert state['blocks'][0]['state'] == 'planned'
    before = w.conn.execute(select(t.study_records)).all()
    w.conn.commit()
    assert book(api_client, w, unit) == state
    assert w.conn.execute(select(t.study_records)).all() == before
    w.conn.commit()
    other = api_client.post('/api/session').json()['data']['anonymousId']
    response = api_client.get('/api/books', headers={'X-Anonymous-Id': other})
    assert response.status_code == 200, response.text
    assert all(u['id'] != unit['id'] for s in response.json()['data']['subjects'] for u in s['units'])
