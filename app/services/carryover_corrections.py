"""Reconcile untouched automatic carry placements after a record correction.

All writes use the record request's user lock and transaction. Historical
placements and ranges remain addressable; only replacement placements are added.
"""
from collections import defaultdict
from fractions import Fraction
from math import ceil
from uuid import uuid4
from types import SimpleNamespace

from sqlalchemy import select, update

from app.db import schema as t
from app.domain.ranges import PageSpan, intersect, normalize, page_count, subtract
from app.services.carryover import _slot

AUTO_REASON = '어제에서 이월'
LIVE = ('planned', 'partial', 'missed')


def _spans(rows):
    return normalize(PageSpan(row['material_id'], row['start_page'], row['end_page'])
        for row in rows if row['start_page'] is not None and row['end_page'] is not None)


def reconcile_carryover(conn, task, *, user_id, at):
    """Recompute this block only, never erasing another remedial obligation."""
    if task.row['plan_id'] is None:
        return []
    tasks = [dict(row) for row in conn.execute(select(t.study_tasks)
        .join(t.subjects, t.subjects.c.id == t.study_tasks.c.subject_id)
        .where(t.study_tasks.c.block_id == task.row['block_id'], t.subjects.c.user_id == user_id)
        .order_by(t.study_tasks.c.scheduled_date, t.study_tasks.c.created_at, t.study_tasks.c.id)).mappings()]
    automatic = [row for row in tasks if row['origin_task_id'] and row['placement_reason'] == AUTO_REASON]
    if not automatic:
        return []
    ids = [row['id'] for row in tasks]
    by_task = defaultdict(list)
    for row in conn.execute(select(t.study_task_ranges, t.units.c.material_id)
        .join(t.units, t.units.c.id == t.study_task_ranges.c.unit_id)
        .where(t.study_task_ranges.c.task_id.in_(ids))
        .order_by(t.study_task_ranges.c.sort_order, t.study_task_ranges.c.id)).mappings():
        by_task[row['task_id']].append(dict(row))
    records = {}
    for row in conn.execute(select(t.study_records).where(t.study_records.c.task_id.in_(ids))
        .order_by(t.study_records.c.updated_at, t.study_records.c.recorded_at, t.study_records.c.id)).mappings():
        records[row['task_id']] = row['id']
    done = [dict(row) for row in conn.execute(select(t.study_record_ranges, t.units.c.material_id)
        .join(t.units, t.units.c.id == t.study_record_ranges.c.unit_id)
        .where(t.study_record_ranges.c.record_id.in_(list(records.values())))).mappings()]
    session_tasks = set(conn.scalars(select(t.study_sessions.c.task_id)
        .where(t.study_sessions.c.task_id.in_(ids))))
    protected = set(records) | session_tasks
    candidates = [row for row in automatic if row['id'] not in protected and row['status'] in LIVE]
    # An earlier correction may have cancelled the entire carry. Reducing that
    # record must restore the obligation with a new row, not revive the old row.
    if not candidates:
        cancelled = [row for row in automatic if row['id'] not in protected and row['status'] == 'excluded']
        if not cancelled:
            return []
        candidates = cancelled[-1:]
    candidate_ids = {row['id'] for row in candidates}
    roots = [row for row in tasks if row['origin_task_id'] not in ids]
    source_ranges = [part for row in roots for part in by_task[row['id']]]
    required = _spans(source_ranges)
    if not required:
        return []  # Unpaged obligations need an explicit completion decision.
    remaining = subtract(required, _spans(done))
    # Existing started/manual placements own their unfinished pages. Their
    # sessions, estimates, ranges and statuses cannot be rewritten by this hook.
    reserved = _spans(part for row in tasks if row['id'] not in candidate_ids and
        row['status'] in (*LIVE, 'completed') for part in by_task[row['id']])
    remaining = subtract(remaining, reserved)
    assignments = {}
    available = remaining
    for row in candidates:
        assignments[row['id']] = intersect(_spans(by_task[row['id']]), available)
        available = subtract(available, assignments[row['id']])
    if available:
        first = candidates[0]['id']
        assignments[first] = normalize((*assignments[first], *available))
    estimates, slots = {}, {}
    for row in candidates:
        desired, old = assignments[row['id']], _spans(by_task[row['id']])
        if desired and (row['status'] not in LIVE or old != desired):
            minutes = ceil(Fraction(row['estimated_minutes'] * page_count(desired), max(1, page_count(old))))
            if not 15 <= minutes <= 60:
                # A record edit is not authority to fabricate a feasible slot.
                # Preserve the outstanding placement for a full replanning pass.
                return []
            slot = _slot(conn, SimpleNamespace(id=row['id'], row=row), row['scheduled_date'], minutes)
            if slot is None:
                return []
            estimates[row['id']], slots[row['id']] = minutes, slot
    created = []
    for row in candidates:
        desired = assignments[row['id']]
        old = _spans(by_task[row['id']])
        if row['status'] in LIVE and old == desired:
            continue
        if not desired:
            conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == row['id']).values(status='excluded'))
            continue
        replacement_id = str(uuid4())
        values = {**row, 'id': replacement_id, 'origin_task_id': row['id'], 'status': 'planned',
                  'starts_at': slots[row['id']][0], 'ends_at': slots[row['id']][1], 'estimated_minutes': estimates[row['id']], 'created_at': at}
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == row['id']).values(status='carried_over'))
        conn.execute(t.study_tasks.insert().values(**values))
        uncovered, order = desired, 0
        for part in [*by_task[row['id']], *source_ranges]:
            for span in intersect(_spans([part]), uncovered):
                conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=replacement_id,
                    unit_id=part['unit_id'], concept_id=part['concept_id'], start_page=span.start,
                    end_page=span.end, sort_order=order))
                order += 1
            uncovered = subtract(uncovered, _spans([part]))
        if uncovered:
            raise ValueError('Carry correction lost required range provenance')
        created.append(replacement_id)
    return created
