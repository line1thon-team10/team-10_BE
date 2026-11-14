"""Immutable weak-concept obligations, with optional legal future placements."""
from collections import defaultdict
from datetime import timedelta
from fractions import Fraction
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.ai.metrics_adapter import current_scope
from app.db import schema as t
from app.core.clock import today_for
from app.domain.ranges import PageSpan, intersect, normalize
from app.domain.scheduling import AllocationInput, BusyInterval, ScopePart, SearchLimit, Subject, UnitScope, WorkRequirement, half_up
from app.repositories.events import EventRepository
from app.services.allocator import allocate
from app.services.onboarding_events import _local, _minute, event_intervals
from app.services.plan_draft import _instant
from app.services.recovery_evidence import unit_ranges, verified_passes
from app.services.study import StudyData


def _unit(conn, user_id, unit_id):
    row = conn.execute(select(t.units, t.materials.c.subject_id, t.materials.c.sha256.label('material_sha256'),
        t.materials.c.analysis_version).join(t.materials, t.units.c.material_id == t.materials.c.id)
        .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id).where(t.units.c.id == unit_id,
            t.subjects.c.user_id == user_id, t.subjects.c.status == 'active', t.units.c.retired_at.is_(None))).mappings().first()
    return dict(row) if row else None


def generate_remedial(conn, user, quiz_id, unit_ids, today, now):
    """Caller holds user lock; source result, obligations and blocks commit together."""
    quiz = conn.execute(select(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz_id,
        t.quiz_sessions.c.user_id == user['id'], t.quiz_sessions.c.status.in_(('submitted','carried_over')),
        t.quiz_sessions.c.finalized_at.is_not(None), t.quiz_sessions.c.grading_status == 'completed')).mappings().first()
    if quiz is None:
        return ()
    created = []
    for uid in sorted(unit_ids):
        unit = _unit(conn, user['id'], uid)
        score = conn.scalar(select(t.mastery_history.c.score).where(t.mastery_history.c.quiz_session_id == quiz_id,
            t.mastery_history.c.unit_id == uid))
        if unit is None or score is None or score >= 80 or quiz['subject_id'] not in (None, unit['subject_id']):
            continue
        concepts = list(conn.execute(select(t.concepts).where(t.concepts.c.unit_id == uid)).mappings())
        scope = unit_ranges(unit, concepts)
        initial = [row for row in verified_passes(conn, user['id'], unit, concepts)
            if row['completion_kind'] == 'initial' and row['completed_date'] <= today]
        if not scope or not initial:
            continue
        identifier = str(uuid5(NAMESPACE_URL, f'team10:remedial:{quiz_id}:{uid}'))
        previous = conn.execute(select(t.remedial_obligations).where(t.remedial_obligations.c.id == identifier)).mappings().first()
        if previous is not None:
            created.append(identifier)
            continue
        wrong = set(conn.scalars(select(t.questions.c.concept_id)
            .join(t.quiz_items, t.quiz_items.c.question_id == t.questions.c.id)
            .join(t.quiz_attempts, t.quiz_attempts.c.quiz_item_id == t.quiz_items.c.id)
            .where(t.quiz_items.c.quiz_session_id == quiz_id, t.questions.c.unit_id == uid,
                t.quiz_attempts.c.is_correct.is_(False), t.quiz_attempts.c.finalized_at.is_not(None),
                t.quiz_attempts.c.graded_at.is_not(None), t.quiz_attempts.c.grading_status.in_(('graded','fallback')))))
        required = intersect(scope, (PageSpan(unit['material_id'], row['start_page'], row['end_page'])
            for row in concepts if row['id'] in wrong and row['in_scope'] and row['retired_at'] is None))
        if not required:
            continue  # Missing concept identity is never guessed from unit number.
        not_before = today + timedelta(days=1) if today.year < 9999 or today.month < 12 or today.day < 31 else today
        conn.execute(t.remedial_obligations.insert().values(id=identifier, user_id=user['id'], unit_id=uid,
            quiz_session_id=quiz_id, scope_revision=current_scope(unit, concepts).revision,
            required_ranges=[[span.start, span.end] for span in required], not_before=not_before, created_at=now))
        schedule_obligation(conn, user, identifier, now)
        created.append(identifier)
    return tuple(created)


def schedule_obligation(conn, user, obligation_id, now):
    """Keep existing appointments/tasks fixed; an infeasible obligation remains stored."""
    obligation = conn.execute(select(t.remedial_obligations).where(t.remedial_obligations.c.id == obligation_id,
        t.remedial_obligations.c.user_id == user['id'])).mappings().first()
    if obligation is None or conn.scalar(select(t.remedial_obligation_blocks.c.block_id).where(
            t.remedial_obligation_blocks.c.obligation_id == obligation_id)) is not None:
        return False
    unit = _unit(conn, user['id'], obligation['unit_id'])
    if unit is None:
        return False
    concepts = list(conn.execute(select(t.concepts).where(t.concepts.c.unit_id == unit['id'])).mappings())
    if current_scope(unit, concepts).revision != obligation['scope_revision']:
        return False
    data = StudyData(conn, user['id'])
    subject = data.subject_by_id.get(unit['subject_id'])
    if (data.plan is None or subject is None or subject['exam_date'] is None
            or subject['exam_date'] <= obligation['not_before'] or subject['target_score'] is None):
        return False
    first = max(obligation['not_before'], today_for(user))
    if first >= subject['exam_date']:
        return False
    study_days = {task.row['scheduled_date'] for task in data.tasks
        if task.row['subject_id'] == subject['id'] and first <= task.row['scheduled_date'] < subject['exam_date']
        and task.row['status'] not in ('excluded','carried_over')}
    first = min(study_days) if study_days else first
    # S02 adds work only to the next subject study day. If that day cannot
    # hold the whole obligation, L01/L02 may replan it without losing ranges.
    last = first + timedelta(days=1)
    zone, busy = ZoneInfo(user['timezone']), []
    if first == today_for(user) and user.get('demo_date') is None:
        current = _local(now, zone)
        if _minute(current) > 8*60:
            busy.append(BusyInterval(first, 8*60, min(23*60, _minute(current))))
    for task in data.tasks:
        row = task.row
        if not first <= row['scheduled_date'] < last or row['status'] in ('excluded','carried_over'):
            continue
        if row['starts_at'] is None or row['ends_at'] is None:
            busy.append(BusyInterval(row['scheduled_date'], 8*60, 23*60))
        else:
            start, end = _local(row['starts_at'], zone), _local(row['ends_at'], zone)
            busy.append(BusyInterval(row['scheduled_date'], _minute(start), _minute(end)))
    repository = EventRepository(conn, user['id']); events = repository.events(); changes = defaultdict(list)
    for row in repository.exceptions([event['id'] for event in events]):
        changes[row['event_id']].append(row)
    try:
        for event in events:
            week = first
            while week < last:
                busy.extend(item for item in event_intervals(event, changes[event['id']], week, user['timezone']) if item.day < last)
                week += timedelta(days=7)
        parts = tuple(ScopePart(unit['id'], PageSpan(unit['material_id'], a, b)) for a, b in obligation['required_ranges'])
        request = AllocationInput(first,
            (Subject(subject['id'], last, subject['target_score'], True, True, subject['minutes_per_page']),),
            (UnitScope(unit['id'], subject['id'], unit['material_id'], unit['sort_order'], (), unit['difficulty'], unit['mastery_score']),),
            tuple(busy), requirements=(WorkRequirement(obligation_id, subject['id'], 'remedial', parts, first),))
        allocation = allocate(request, max_search_nodes=25_000)
    except (ValueError, SearchLimit, OverflowError):
        return False
    if not allocation.feasible:
        return False
    for placement in allocation.placements:
        block_id, task_id = str(uuid4()), str(uuid4())
        conn.execute(t.study_blocks.insert().values(id=block_id, subject_id=subject['id'], unit_id=unit['id'],
            task_type='remedial', created_at=now))
        conn.execute(t.remedial_obligation_blocks.insert().values(obligation_id=obligation_id, block_id=block_id))
        conn.execute(t.study_tasks.insert().values(id=task_id, plan_id=data.plan['id'], subject_id=subject['id'],
            block_id=block_id, task_type='remedial', title=unit['title']+' 보충', scheduled_date=placement.day,
            starts_at=_instant(placement.day.isoformat(), [placement.start.numerator, placement.start.denominator], user['timezone']),
            ends_at=_instant(placement.day.isoformat(), [placement.end.numerator, placement.end.denominator], user['timezone']),
            estimated_minutes=placement.display_minutes, placement_reason='틀린 개념 보충 · allocation-v1', status='planned', created_at=now))
        for order, part in enumerate(placement.parts):
            conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=task_id, unit_id=part.unit_id,
                start_page=part.span.start, end_page=part.span.end, sort_order=order, planned_minutes=half_up(part.minutes)))
    if allocation.placements:
        conn.execute(update(t.plans).where(t.plans.c.id == data.plan['id']).values(revision=t.plans.c.revision+1))
    return bool(allocation.placements)


def after_initial_passes(conn, user_id, unit_ids, at):
    """Handle quiz-before-first-pass order using only the latest unit result."""
    if not unit_ids:
        return
    user = conn.execute(select(t.users).where(t.users.c.id == user_id)).mappings().first()
    if user is None:
        return
    user = dict(user)
    for unit_id in sorted(set(unit_ids)):
        latest = conn.execute(select(t.mastery_history).join(t.quiz_sessions,
            t.mastery_history.c.quiz_session_id == t.quiz_sessions.c.id).where(
                t.mastery_history.c.unit_id == unit_id, t.quiz_sessions.c.user_id == user_id,
                t.quiz_sessions.c.status.in_(('submitted','carried_over')),
                t.quiz_sessions.c.grading_status == 'completed', t.quiz_sessions.c.finalized_at.is_not(None))
            .order_by(t.mastery_history.c.recorded_date.desc(), t.mastery_history.c.created_at.desc(), t.mastery_history.c.id.desc())
            .limit(1)).mappings().first()
        if latest and latest['score'] < 80:
            generate_remedial(conn, user, latest['quiz_session_id'], {unit_id}, today_for(user), at)
