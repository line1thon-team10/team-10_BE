"""Verify durable learning and custom-quiz facts before granting recovery caps."""
from collections import defaultdict
import re
from uuid import UUID

from sqlalchemy import select

from app.ai.metrics_adapter import current_scope
from app.db import schema as t
from app.domain.mastery import DayEligibility
from app.domain.ranges import PageSpan, intersect, normalize, subtract
from app.services.learning_evidence import _required_pages


def unit_ranges(unit, concepts):
    """Current included page scope; never assign nominal pages to time units."""
    if not unit['in_scope'] or unit['retired_at'] is not None or unit['quantity_basis'] != 'page':
        return ()
    children = [row for row in concepts if row['unit_id'] == unit['id'] and row['retired_at'] is None]
    selected = [row for row in children if row['in_scope']] if children else [unit]
    try:
        return normalize(PageSpan(unit['material_id'], row['start_page'], row['end_page']) for row in selected)
    except ValueError:
        return ()


def verified_passes(conn, user_id, unit, concepts, *, require_current_scope=True):
    """A row flag alone cannot establish a pass: recheck its frozen evidence."""
    scope = unit_ranges(unit, concepts)
    if not scope and require_current_scope:
        return []
    revision = current_scope(unit, concepts).revision
    rows = [dict(row) for row in conn.execute(select(t.learning_passes).where(
        t.learning_passes.c.unit_id == unit['id'])).mappings()]
    valid = []
    for row in rows:
        snapshot = row['evidence_snapshot']
        if row['invalidated_at'] is not None or not isinstance(snapshot, dict):
            continue
        if snapshot.get('materialId') != unit['material_id'] and not (row['completion_kind']=='initial' and 'materialId' not in snapshot):
            continue
        try:
            required = normalize(PageSpan(unit['material_id'], a, b) for a, b in _required_pages(snapshot))
            ids = snapshot.get('recordIds')
            if not isinstance(ids, list) or len(ids) != len(set(ids)):
                continue
            ids = [str(UUID(value)) for value in ids]
        except (ValueError, TypeError, AttributeError):
            continue
        current = row['completion_kind'] != 'initial' and (require_current_scope or row['completion_kind'] != 'full_relearn')
        if current and snapshot.get('masteryScopeRevision') != revision:
            continue
        if not required or (current and subtract(required, scope)):
            continue
        if current and row['completion_kind'] == 'full_relearn' and required != scope:
            continue
        if row['completion_kind'] == 'full_relearn' and row['evidence_key'].startswith('direct:'):
            key, digest = row['evidence_key'], snapshot.get('directRequestHash')
            if not re.fullmatch('[a-f0-9]{64}', key[7:]) or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                continue
        if not ids:
            continue
        if any(other['id'] != row['id'] and isinstance(other['evidence_snapshot'], dict)
               and isinstance(other['evidence_snapshot'].get('recordIds'), list)
               and all(isinstance(value, str) for value in other['evidence_snapshot']['recordIds'])
               and set(ids).intersection(other['evidence_snapshot']['recordIds']) for other in rows):
            continue
        records = [dict(item) for item in conn.execute(select(t.study_records, t.study_tasks.c.subject_id,
            t.study_tasks.c.block_id, t.study_tasks.c.task_type).join(t.study_tasks, t.study_records.c.task_id == t.study_tasks.c.id)
            .join(t.subjects, t.study_tasks.c.subject_id == t.subjects.c.id).where(
                t.study_records.c.id.in_(ids), t.subjects.c.user_id == user_id,
                t.study_tasks.c.subject_id == unit['subject_id'], t.study_records.c.result.in_(('completed','partial')),
                t.study_records.c.study_date <= row['completed_date'])).mappings()]
        if {item['id'] for item in records} != set(ids):
            continue
        if row['completion_kind'] == 'initial' and any(item['task_type'] != 'study' for item in records):
            continue
        if row['completion_kind'] == 'remedial':
            try:
                obligation_id = str(UUID(snapshot.get('obligationId')))
            except (ValueError, TypeError, AttributeError):
                continue
            obligation = conn.execute(select(t.remedial_obligations).where(
                t.remedial_obligations.c.id == obligation_id, t.remedial_obligations.c.user_id == user_id,
                t.remedial_obligations.c.unit_id == unit['id'])).mappings().first()
            if obligation is None or row['evidence_key'] != obligation_id or obligation['scope_revision'] != revision:
                continue
            blocks = set(conn.scalars(select(t.remedial_obligation_blocks.c.block_id).where(
                t.remedial_obligation_blocks.c.obligation_id == obligation_id)))
            if (not blocks or snapshot.get('sourceQuizId') != obligation['quiz_session_id']
                    or not isinstance(snapshot.get('blockIds'), list) or set(snapshot['blockIds']) != blocks
                    or any(item['block_id'] not in blocks or item['task_type'] != 'remedial' for item in records)):
                continue
            try:
                original = normalize(PageSpan(unit['material_id'], a, b) for a, b in obligation['required_ranges'])
            except (ValueError, TypeError):
                continue
            if original != required:
                continue
        actual, on_day = [], []
        dates = {item['id']: item['study_date'] for item in records}
        for item in conn.execute(select(t.study_record_ranges).where(t.study_record_ranges.c.record_id.in_(ids),
                t.study_record_ranges.c.unit_id == unit['id'])).mappings():
            if item['start_page'] is None or item['end_page'] is None:
                continue
            span = PageSpan(unit['material_id'], item['start_page'], item['end_page'])
            actual.append(span)
            if dates[item['record_id']] == row['completed_date']:
                on_day.append(span)
        if subtract(required, actual):
            continue
        valid.append({**row, 'sameDayFull': not subtract(scope, on_day)})
    return valid


def recovery_eligibility(conn, user_id, data):
    """Return exact current-scope/day facts, without changing historical results."""
    result = []
    for unit in data['units']:
        passes = verified_passes(conn, user_id, unit, data['concepts'])
        scope = unit_ranges(unit, data['concepts'])
        if not passes or not scope:
            continue
        initial = first_passes(conn, user_id, unit, data['concepts'])
        days = defaultdict(lambda: {'full_relearned': False, 'custom_confirmed': False, 'supplement_completed': False})
        for row in passes:
            if row['completion_kind'] == 'remedial' and any(p['completed_date'] <= row['completed_date'] for p in initial):
                days[row['completed_date']]['supplement_completed'] = True
            if row['completion_kind'] == 'full_relearn' and row['sameDayFull']:
                days[row['completed_date']]['full_relearned'] = True
        custom = conn.execute(select(t.quiz_sessions.c.id, t.quiz_attempts.c.answered_date)
            .join(t.quiz_items, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .join(t.quiz_attempts, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
            .join(t.questions, t.quiz_items.c.question_id == t.questions.c.id)
            .where(t.quiz_sessions.c.user_id == user_id,
                (t.quiz_sessions.c.subject_id.is_(None) | (t.quiz_sessions.c.subject_id == unit['subject_id'])), t.quiz_sessions.c.type.in_(('custom','practice')),
                t.quiz_sessions.c.status == 'submitted', t.quiz_sessions.c.finalized_at.is_not(None),
                t.quiz_sessions.c.grading_status == 'completed', t.questions.c.unit_id == unit['id'],
                t.quiz_attempts.c.finalized_at.is_not(None), t.quiz_attempts.c.graded_at.is_not(None),
                t.quiz_attempts.c.grading_status.in_(('graded','fallback')), t.quiz_attempts.c.is_correct.is_not(None))).all()
        for quiz_id, day in custom:
            if day not in days:
                continue
            rows = conn.execute(select(t.quiz_scopes).where(t.quiz_scopes.c.quiz_session_id == quiz_id,
                t.quiz_scopes.c.unit_id == unit['id'])).mappings().all()
            selected = normalize(PageSpan(unit['material_id'], row['start_page'], row['end_page']) for row in rows)
            if selected == scope:
                days[day]['custom_confirmed'] = True
        for day, values in sorted(days.items()):
            result.append(DayEligibility(current_scope(unit, data['concepts']), day, **values))
    return tuple(result)


def first_passes(conn, user_id, unit, concepts):
    """Initial/full re-study first-pass facts keep their original complete scope."""
    return [row for row in verified_passes(conn, user_id, unit, concepts, require_current_scope=False)
            if row['completion_kind'] in ('initial', 'full_relearn')]
