"""Exact pace evidence from completed sessions and immutable placement factors."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from fractions import Fraction

from sqlalchemy import select

from app.db import schema as t
from app.domain.ranges import PageSpan, intersect, normalize, page_count
from app.domain.scheduling import exact
from app.services.study import StudyData


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fraction(value):
    if not isinstance(value, list) or len(value) != 2 or any(type(x) is not int for x in value):
        raise ValueError('An exact fraction is required')
    value = Fraction(*value)
    if value <= 0:
        raise ValueError('A positive factor is required')
    return value


def encoded(value):
    return [value.numerator, value.denominator]


def placements(value):
    """Known immutable envelope shapes, never infer factors from mutable units."""
    if not isinstance(value, dict):
        return []
    if value.get('schemaVersion') == 'plan-placements-v1':
        return value.get('placements', [])
    if value.get('schemaVersion') == 'pace-adjustment-v1':
        return value.get('placements', [])
    return placements(value.get('allocation'))


def placement_snapshots(conn, user_id):
    result = defaultdict(list)
    rows = conn.execute(select(t.plan_proposals.c.changes, t.plan_proposal_states.c.decided_at)
        .join(t.plan_proposal_states, t.plan_proposals.c.id == t.plan_proposal_states.c.proposal_id)
        .join(t.plan_change_requests, t.plan_proposals.c.request_id == t.plan_change_requests.c.id)
        .where(t.plan_proposal_states.c.user_id == user_id, t.plan_change_requests.c.user_id == user_id,
            t.plan_proposal_states.c.status == 'applied', t.plan_change_requests.c.status == 'accepted',
            t.plan_change_requests.c.accepted_proposal_id == t.plan_proposals.c.id,
            t.plan_proposal_states.c.decided_at.is_not(None))).mappings()
    for row in rows:
        for placement in placements(row['changes']):
            if isinstance(placement, dict) and isinstance(placement.get('id'), str):
                result[placement['id']].append((aware(row['decided_at']), placement))
    for values in result.values():
        values.sort(key=lambda value: value[0])
    return result


def snapshot_for_task(task, tasks, snapshots, at):
    """Carry/move copies inherit only factors covering the same owned block ranges."""
    original, seen = task, set()
    while original is not None and original.id not in seen:
        seen.add(original.id)
        available = [value for decided, value in snapshots.get(original.id, []) if decided <= aware(at)]
        if available:
            source = available[-1]
            if original.id == task.id:
                return source
            parts=[]
            try:
                for row in task.paged:
                    matching=[part for part in source['parts'] if part['unitId']==row['unit_id']
                        and part['materialId']==row['material_id'] and part['from']<=row['end_page'] and part['to']>=row['start_page']]
                    expected=[PageSpan(row['material_id'],row['start_page'],row['end_page'])]
                    covered=intersect(expected,[PageSpan(row['material_id'],part['from'],part['to']) for part in matching])
                    factors={(fraction(part['baseline']),fraction(part['multiplier'])) for part in matching}
                    if page_count(covered)!=page_count(expected) or len(factors)!=1:
                        return None
                    baseline,multiplier=factors.pop()
                    parts.append({'unitId':row['unit_id'],'materialId':row['material_id'],
                        'from':row['start_page'],'to':row['end_page'],'baseline':encoded(baseline),'multiplier':encoded(multiplier)})
            except (ValueError,KeyError,TypeError,ZeroDivisionError):
                return None
            return {'id':task.id,'parts':parts}
        parent=tasks.get(original.row['origin_task_id'])
        if parent is not None and (parent.row['block_id']!=task.row['block_id']
                or parent.row['subject_id']!=task.row['subject_id'] or parent.row['plan_id']!=task.row['plan_id']):
            return None
        original=parent
    return None


def task_factors(task, snapshot):
    """Only snapshots matching the actual immutable task ranges are admissible."""
    parts = snapshot.get('parts')
    if not isinstance(parts, list) or not parts:
        raise ValueError('No factor snapshot')
    def identity(row):
        return row['unitId'], row['materialId'], row['from'], row['to']
    actual = {(row['unit_id'], row['material_id'], row['start_page'], row['end_page']) for row in task.paged}
    expected = {identity(row) for row in parts}
    if expected != actual or len(task.paged) != len(task.ranges):
        raise ValueError('Snapshot range mismatch')
    return [(part, fraction(part['multiplier']), fraction(part['baseline'])) for part in parts]


def weighted_pages(task, factors, *, completed):
    """Count distinct physical pages once; conflicting multipliers are unverifiable."""
    segments = defaultdict(list)
    for part, multiplier, _ in factors:
        bounds = [PageSpan(part['materialId'], part['from'], part['to'])]
        if completed:
            done = [PageSpan(part['materialId'], r['start_page'], r['end_page']) for r in task.done_ranges
                if r['unit_id'] == part['unitId'] and r['start_page'] is not None and r['end_page'] is not None]
            bounds = intersect(bounds, done)
        for span in bounds:
            segments[span.material_id].append((span.start, span.end, multiplier))
    count, weighted = 0, Fraction(0)
    for values in segments.values():
        endpoints = sorted({point for start, end, _ in values for point in (start, end + 1)})
        for start, end in zip(endpoints, endpoints[1:]):
            factors = {factor for left, right, factor in values if left <= start <= right}
            if len(factors) > 1:
                raise ValueError('Overlapping pages have contradictory historical factors')
            if factors:
                count += end-start
                weighted += (end-start) * next(iter(factors))
    return count, weighted


@dataclass(frozen=True)
class PaceSamples:
    samples: tuple[dict, ...]
    measured_seconds: int
    completed_pages: int
    weighted_pages: Fraction
    baseline: Fraction

    @property
    def days(self):
        return len({sample['date'] for sample in self.samples})

    @property
    def proposed(self):
        return Fraction(self.measured_seconds, 60) / self.weighted_pages

    @property
    def ratio(self):
        return self.proposed / self.baseline

    @property
    def eligible(self):
        return self.days == 3 and self.weighted_pages > 0 and abs(self.ratio - 1) >= Fraction(1, 5)


def collect_samples(conn, user, subject, today, *, data=None, snapshots=None):
    data = data or StudyData(conn, user['id'])
    snapshots = snapshots if snapshots is not None else placement_snapshots(conn, user['id'])
    start = today - timedelta(days=min(2, (today-date.min).days))
    sessions = {row['id']: dict(row) for row in conn.execute(select(t.study_sessions).where(
        t.study_sessions.c.user_id == user['id'], t.study_sessions.c.status == 'ended',
        t.study_sessions.c.excluded_from_calibration.is_(False),
        t.study_sessions.c.study_date >= start, t.study_sessions.c.study_date <= today)).mappings()}
    samples, seen, seconds, pages, weighted = [], set(), 0, 0, Fraction(0)
    tasks_by_id={task.id:task for task in data.tasks}
    for task in data.tasks:
        record = task.record
        if (task.row['subject_id'] != subject['id'] or task.row['status'] == 'excluded'
                or task.row['task_type'] not in ('study','remedial') or not record):
            continue
        session = sessions.get(record['session_id'])
        if (not session or session['id'] in seen or session['task_id'] != task.id
                or not session['measured_seconds'] or session['measured_seconds'] <= 0):
            continue
        snapshot = snapshot_for_task(task,tasks_by_id,snapshots,session['started_at'])
        if snapshot is None:
            continue
        try:
            factors = task_factors(task, snapshot)
            count, weighted_count = weighted_pages(task, factors, completed=True)
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            continue
        if count == 0 or weighted_count <= 0:
            continue
        seen.add(session['id'])
        samples.append({'sessionId':session['id'], 'recordId':record['id'], 'taskId':task.id,
            'date':session['study_date'].isoformat(), 'seconds':session['measured_seconds'], 'pages':count,
            'weightedPages':encoded(weighted_count), 'parts':snapshot['parts']})
        seconds += session['measured_seconds']; pages += count; weighted += weighted_count
    samples.sort(key=lambda item: (item['date'], item['sessionId']))
    return PaceSamples(tuple(samples), seconds, pages, weighted, exact(subject['minutes_per_page']))
