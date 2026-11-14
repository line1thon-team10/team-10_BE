"""Record PATCH reconciles auto carry without rewriting later learning facts."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_progress_contract import progress_world


def carry(w, origin, parts, *, day=1, reason='어제에서 이월', status='planned'):
    source = dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == origin)).mappings().one())
    tid = str(uuid4())
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == origin).values(status='carried_over'))
    w.conn.execute(t.study_tasks.insert().values(**{**source, 'id': tid, 'origin_task_id': origin,
        'placement_reason': reason, 'scheduled_date': w.day + timedelta(days=day), 'status': status}))
    for index, (unit, start, end) in enumerate(parts):
        w.conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=tid, unit_id=unit['id'],
            start_page=start, end_page=end, sort_order=index))
    return tid


def patch(client, w, task, **body):
    w.conn.commit()
    result = client.patch(f'/api/tasks/{task}/record', headers={'X-Anonymous-Id': w.user}, json=body)
    assert result.status_code == 200, result.text
    return result


def rows(w, task):
    return [dict(r) for r in w.conn.execute(select(t.study_task_ranges)
        .where(t.study_task_ranges.c.task_id == task).order_by(t.study_task_ranges.c.sort_order)).mappings()]


def current(w, root):
    block = w.conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id == root))
    return [dict(row) for row in w.conn.execute(select(t.study_tasks).where(
        t.study_tasks.c.block_id == block, t.study_tasks.c.status == 'planned')).mappings()]


def test_correction_shrinks_append_only_and_repeated_patch_adds_nothing(api_client, progress_world):
    w = progress_world
    u = w.unit(1, 20)
    root = w.task((u, 1, 20), done=[(u, 1, 5)], status='partial')
    child = carry(w, root, [(u, 6, 20)])
    original, child_ranges = rows(w, root), rows(w, child)
    patch(api_client, w, root, toPage=10)
    live = current(w, root)
    assert len(live) == 1 and live[0]['origin_task_id'] == child
    assert [(r['start_page'], r['end_page']) for r in rows(w, live[0]['id'])] == [(11, 20)]
    assert live[0]['estimated_minutes'] == 17
    assert live[0]['starts_at'] is not None and live[0]['ends_at'] - live[0]['starts_at'] == timedelta(minutes=17)
    assert rows(w, root) == original and rows(w, child) == child_ranges
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == child)) == 'carried_over'
    patch(api_client, w, root, toPage=10)
    assert [r['id'] for r in current(w, root)] == [live[0]['id']]


def test_full_correction_cancels_then_decrease_restores_new_placement(api_client, progress_world):
    w = progress_world
    u = w.unit(1, 20)
    root = w.task((u, 1, 20), done=[(u, 1, 5)], status='partial')
    child = carry(w, root, [(u, 6, 20)])
    before = rows(w, child)
    patch(api_client, w, root, toPage=20)
    assert not current(w, root)
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == child)) == 'excluded'
    patch(api_client, w, root, toPage=3)
    live = current(w, root)
    assert len(live) == 1 and live[0]['origin_task_id'] == child
    assert [(r['start_page'], r['end_page']) for r in rows(w, live[0]['id'])] == [(6, 20), (4, 5)]
    assert rows(w, child) == before


def test_disjoint_duplicate_ranges_and_distinct_materials_stay_exact(api_client, progress_world):
    w = progress_world
    a, b = w.unit(1, 20), w.unit(1, 20)
    root = w.task((a, 1, 5), (a, 4, 8), (a, 12, 15), (b, 1, 5), done=[])
    child = carry(w, root, [(a, 1, 8), (a, 12, 15), (b, 1, 5)])
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == child).values(estimated_minutes=40))
    selected = rows(w, root)
    patch(api_client, w, root, completedRanges=[{'rangeId': selected[0]['id'], 'toPage': 5},
        {'rangeId': selected[1]['id'], 'toPage': 6}, {'rangeId': selected[3]['id'], 'toPage': 2}])
    live = current(w, root)
    assert len(live) == 1
    assert {(r['unit_id'], r['start_page'], r['end_page']) for r in rows(w, live[0]['id'])} == {
        (a['id'], 7, 8), (a['id'], 12, 15), (b['id'], 3, 5)}


@pytest.mark.parametrize('fact', ['session', 'record'])
def test_later_started_or_recorded_child_is_immutable(api_client, progress_world, fact):
    w = progress_world
    u = w.unit(1, 20)
    root = w.task((u, 1, 20), done=[(u, 1, 5)], status='partial')
    child = carry(w, root, [(u, 6, 20)])
    if fact == 'session':
        w.conn.execute(t.study_sessions.insert().values(id=str(uuid4()), user_id=w.user, task_id=child,
            status='ended', started_at=w.now, ended_at=w.now+timedelta(minutes=15), measured_seconds=900,
            study_date=w.day+timedelta(days=1), created_at=w.now))
    else:
        rid = str(uuid4())
        w.conn.execute(t.study_records.insert().values(id=rid, task_id=child, result='partial',
            recorded_at=w.now, updated_at=w.now, study_date=w.day+timedelta(days=1)))
        w.conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=rid,
            unit_id=u['id'], start_page=6, end_page=10))
    before = dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == child)).mappings().one())
    ranges = rows(w, child)
    patch(api_client, w, root, toPage=20)
    assert dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == child)).mappings().one()) == before
    assert rows(w, child) == ranges


def test_multicarry_uses_all_latest_block_records_and_preserves_other_remedial_block(api_client, progress_world):
    w = progress_world
    u = w.unit(1, 20)
    root = w.task((u, 1, 20), done=[(u, 1, 5)], status='partial')
    first = carry(w, root, [(u, 6, 20)])
    rid = str(uuid4())
    w.conn.execute(t.study_records.insert().values(id=rid, task_id=first, result='partial',
        recorded_at=w.now, updated_at=w.now, study_date=w.day+timedelta(days=1)))
    w.conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=rid,
        unit_id=u['id'], start_page=11, end_page=15))
    second = carry(w, first, [(u, 6, 10), (u, 16, 20)], day=2)
    remedial = w.task((u, 1, 20), kind='remedial')
    remedial_child = carry(w, remedial, [(u, 1, 20)])
    remedial_before = rows(w, remedial_child)
    patch(api_client, w, root, toPage=8)
    live = current(w, root)
    assert len(live) == 1 and live[0]['origin_task_id'] == second
    assert [(r['start_page'], r['end_page']) for r in rows(w, live[0]['id'])] == [(9, 10), (16, 20)]
    assert rows(w, remedial_child) == remedial_before
    assert w.conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == remedial_child)) == 'planned'


def test_hook_failure_rolls_back_record_and_carry_changes(api_client, progress_world, monkeypatch):
    from app.services import records
    w = progress_world
    u = w.unit(1, 20)
    root = w.task((u, 1, 20), done=[(u, 1, 5)], status='partial')
    child = carry(w, root, [(u, 6, 20)])
    before = rows(w, child)
    w.conn.commit()
    real = records.reconcile_carryover
    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('after replacements')
    monkeypatch.setattr(records, 'reconcile_carryover', fail)
    result = api_client.patch(f'/api/tasks/{root}/record', headers={'X-Anonymous-Id': w.user}, json={'toPage': 20})
    assert result.status_code == 500
    assert [row['id'] for row in current(w, root)] == [child]
    assert rows(w, child) == before
    rid = w.conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == root))
    assert w.conn.scalar(select(t.study_record_ranges.c.end_page).where(t.study_record_ranges.c.record_id == rid)) == 5


def test_duplicate_live_carries_are_rejected_before_corrupting_required_pages(api_client, progress_world):
    from sqlalchemy.exc import IntegrityError
    w = progress_world
    u = w.unit(1, 20)
    root = w.task((u, 1, 20), done=[(u, 1, 5)], status='partial')
    first = carry(w, root, [(u, 6, 20)])
    original = rows(w, first)
    with pytest.raises(IntegrityError), w.conn.begin_nested():
        carry(w, root, [(u, 6, 20)], day=2)
    assert [row['id'] for row in current(w, root)] == [first]
    assert rows(w, first) == original


def test_same_pdf_overlapping_units_share_completed_pages(api_client, progress_world):
    w = progress_world
    first = w.unit(1, 20)
    w.conn.execute(update(t.units).where(t.units.c.id == first['id']).values(sort_order=2))
    second = w.unit(1, 20, material_id=first['material_id'])
    root = w.task((first, 1, 10), (second, 6, 20), done=[(first, 1, 5)], status='partial')
    child = carry(w, root, [(second, 6, 20)])
    rid = rows(w, root)[0]['id']
    patch(api_client, w, root, completedRanges=[{'rangeId': rid, 'toPage': 10}])
    live = current(w, root)
    assert len(live) == 1
    assert [(r['unit_id'], r['start_page'], r['end_page']) for r in rows(w, live[0]['id'])] == [(second['id'], 11, 20)]


@pytest.mark.parametrize('initial_end,carry_start,minutes,new_end', [(5, 6, 25, 19), (19, 20, 15, 1)])
def test_outside_15_to_60_minutes_defers_replacement_without_fabricating_duration(
        api_client, progress_world, initial_end, carry_start, minutes, new_end):
    w = progress_world
    u = w.unit(1, 20)
    root = w.task((u, 1, 20), done=[(u, 1, initial_end)], status='partial')
    child = carry(w, root, [(u, carry_start, 20)])
    w.conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == child).values(estimated_minutes=minutes))
    before = dict(w.conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == child)).mappings().one())
    ranges = rows(w, child)
    patch(api_client, w, root, toPage=new_end)
    assert current(w, root) == [before]
    assert rows(w, child) == ranges
