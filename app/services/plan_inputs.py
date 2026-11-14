"""Read initial draft inputs for allocation-v1 without approving or writing.

The caller owns its transaction/user lock and captures the input versions before
calculating a preview. Existing-plan reallocation is a separate boundary.
"""
from collections import defaultdict
from dataclasses import replace
from fractions import Fraction
from zoneinfo import ZoneInfo
from datetime import timezone
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select

from app.db import schema as t
from app.domain.onboarding import CONFIRMATION_COLUMNS, subject_view
from app.domain.ranges import PageSpan, subtract
from app.domain.scheduling import (AllocationInput, Subject, UnitScope, FixedTask, FixedRange,
    free_slots)
from app.repositories.events import EventRepository
from app.services.onboarding_events import event_intervals


class PlanInputError(ValueError):
    """An internal preparation failure; the API owns public error mapping."""


class PendingObligations(PlanInputError):
    """Overdue remedial/review obligations need the carryover flow first."""


def _page_bounds(start, end, maximum):
    if (type(start) is not int or type(end) is not int or type(maximum) is not int
            or not 1 <= start <= end <= maximum):
        raise PlanInputError("실제 PDF 안의 올바른 페이지 범위를 확인해 주세요")


def initial_allocation_input(connection, user_id: str, *, today: date,
                             timezone_name: str, subject_ids: tuple[str, ...] | None = None) -> AllocationInput:
    return _allocation_input(connection, user_id, today=today, timezone_name=timezone_name,
                             subject_ids=subject_ids, existing=False)


def _allocation_input(connection, user_id, *, today, timezone_name, subject_ids, existing):
    """Resolve owned draft subjects, concept unions and actual calendar intervals.

    quiz_enabled is deliberately not an admission condition: a manual outline
    without question evidence can still provide a valid page learning scope.
    This function never silently includes an active or foreign subject.
    """
    if type(today) is not date:
        raise PlanInputError("사용자 현지 날짜가 필요해요")
    if not existing and connection.scalar(select(t.plans.c.id).where(t.plans.c.user_id == user_id)) is not None:
        raise PlanInputError("승인된 계획의 재배분은 초기 계획 입력과 구분해야 해요")
    rows = [dict(row) for row in connection.execute(select(t.subjects).where(
        t.subjects.c.user_id == user_id, t.subjects.c.status == "draft"
    ).order_by(t.subjects.c.id)).mappings()]
    if subject_ids is not None:
        if not isinstance(subject_ids, tuple) or not subject_ids or any(type(item) is not str for item in subject_ids):
            raise PlanInputError("선택할 draft 과목 ID 목록이 필요해요")
        try:
            selected = tuple(str(UUID(item)) for item in subject_ids)
        except ValueError as exc:
            raise PlanInputError("올바른 과목 UUID가 필요해요") from exc
        if len(set(selected)) != len(selected) or not set(selected) <= {row["id"] for row in rows}:
            raise PlanInputError("중복 없이 본인의 draft 과목을 선택해 주세요")
        rows = [row for row in rows if row["id"] in selected]
    if not rows:
        raise PlanInputError("계획할 draft 과목이 필요해요")
    if existing:
        rows += [dict(row) for row in connection.execute(select(t.subjects).where(
            t.subjects.c.user_id == user_id, t.subjects.c.status == 'active',
            t.subjects.c.exam_date > today).order_by(t.subjects.c.id)).mappings()]
    subjects = []
    for row in rows:
        if (row["status"] == "draft" and (not all(row[column] is not None for column in CONFIRMATION_COLUMNS.values())
                or subject_view(row)["status"] != "confirmed" or row["target_score"] is None)):
            raise PlanInputError("시험과 목표 네 칸을 확인해 주세요")
        if row["exam_date"] <= today:
            raise PlanInputError("시험일은 오늘보다 뒤여야 해요")
        try:
            subjects.append(Subject(row["id"], row["exam_date"], row["target_score"], True, True,
                                    row["minutes_per_page"]))
        except ValueError as exc:
            raise PlanInputError("목표 점수와 기준 속도를 확인해 주세요") from exc
    selected = tuple(row["id"] for row in rows)
    materials = [dict(row) for row in connection.execute(select(t.materials).where(
        t.materials.c.subject_id.in_(selected)
    ).order_by(t.materials.c.id)).mappings()]
    by_material = {row["id"]: row for row in materials}
    if {row["subject_id"] for row in materials} != set(selected):
        raise PlanInputError("각 과목의 PDF 자료가 필요해요")
    unit_rows = [dict(row) for row in connection.execute(select(t.units).where(
        t.units.c.material_id.in_(by_material), t.units.c.retired_at.is_(None)
    ).order_by(t.units.c.material_id, t.units.c.sort_order, t.units.c.id)).mappings()]
    if {row["material_id"] for row in unit_rows} != set(by_material):
        raise PlanInputError("자료별 페이지 목차를 확인해 주세요")
    concepts = defaultdict(list)
    for row in connection.execute(select(t.concepts).where(
            t.concepts.c.unit_id.in_([unit["id"] for unit in unit_rows]), t.concepts.c.retired_at.is_(None)
    ).order_by(t.concepts.c.unit_id, t.concepts.c.sort_order, t.concepts.c.id)).mappings():
        concepts[row["unit_id"]].append(dict(row))
    scopes, bounds = [], defaultdict(list)
    for row in unit_rows:
        material = by_material[row["material_id"]]
        if row["quantity_basis"] != "page":
            raise PlanInputError("초기 계획에는 실제 페이지 목차가 필요해요")
        _page_bounds(row["start_page"], row["end_page"], material["page_count"])
        bounds[material["id"]].append((row["start_page"], row["end_page"]))
        children = concepts[row["id"]]
        for child in children:
            _page_bounds(child["start_page"], child["end_page"], row["end_page"])
            if child["start_page"] < row["start_page"]:
                raise PlanInputError("개념은 소속 단원 범위 안에 있어야 해요")
        spans = ()
        if row["in_scope"]:
            spans = (tuple(PageSpan(material["id"], child["start_page"], child["end_page"])
                           for child in children if child["in_scope"]) if children else
                     (PageSpan(material["id"], row["start_page"], row["end_page"]),))
        try:
            scopes.append(UnitScope(row["id"], material["subject_id"], material["id"], row["sort_order"],
                                    spans, row["difficulty"], row["mastery_score"]))
        except ValueError as exc:
            raise PlanInputError("단원의 난이도와 측정값을 확인해 주세요") from exc
    for ranges in bounds.values():
        ordered = sorted(ranges)
        if any(left[1] >= right[0] for left, right in zip(ordered, ordered[1:])):
            raise PlanInputError("같은 PDF의 단원 범위가 겹쳐요")
    if {unit.subject_id for unit in scopes if unit.ranges} != set(selected):
        raise PlanInputError("각 과목에서 공부할 시험 범위를 선택해 주세요")

    repository = EventRepository(connection, user_id)
    events = repository.events()
    exceptions = defaultdict(list)
    for row in repository.exceptions([event["id"] for event in events]):
        exceptions[row["event_id"]].append(row)
    last_exam = max(subject.exam_date for subject in subjects)
    busy = []
    try:
        for event in events:
            week = today
            while week < last_exam:
                busy.extend(interval for interval in event_intervals(event, exceptions[event["id"]], week, timezone_name)
                            if today <= interval.day < last_exam)
                week += timedelta(days=7)
    except (ValueError, KeyError, OverflowError) as exc:
        raise PlanInputError("저장된 일정의 반복 규칙과 날짜를 확인해 주세요") from exc
    return AllocationInput(today, tuple(subjects), tuple(scopes), tuple(busy))


def existing_allocation_input(connection, user_id, *, today, timezone_name, subject_ids):
    """Keep learning facts and active started placements while reallocating rest.

    Only selected draft subjects are added. Every active study range is reduced
    by actual recorded pages; remedial/review ranges remain separate obligations.
    Superseded rows are retained (including records, quiz/book references).
    """
    if not subject_ids:
        raise PlanInputError('추가할 draft 과목을 선택해 주세요')
    request = _allocation_input(connection, user_id, today=today, timezone_name=timezone_name,
                                subject_ids=subject_ids, existing=True)
    from app.services.study import StudyData
    data = StudyData(connection, user_id)
    included = {subject.id for subject in request.subjects}
    catalog = {unit.id: unit for unit in request.units}
    sessions = defaultdict(list)
    for row in connection.execute(select(t.study_sessions).where(t.study_sessions.c.user_id == user_id)).mappings():
        sessions[row['task_id']].append(dict(row))
    consumed_sessions = set(connection.scalars(select(t.study_records.c.session_id).where(
        t.study_records.c.task_id.in_([task.id for task in data.tasks]), t.study_records.c.session_id.is_not(None))))
    learned, fixed, supersede = defaultdict(list), [], []
    pending_fixed = []
    for task in data.tasks:
        if task.row['subject_id'] not in included:
            continue
        if task.record and task.record['result'] in ('completed', 'partial'):
            for span in task.done_ranges:
                if span['unit_id'] in catalog and span['start_page'] is not None and span['end_page'] is not None:
                    unit = catalog[span['unit_id']]
                    if task.row['task_type'] == 'study':
                        learned[unit.id].append(PageSpan(unit.material_id, span['start_page'], span['end_page']))
        if task.row['status'] in ('carried_over', 'excluded'):
            continue
        if task.row['task_type'] in ('remedial', 'review') and task.row['scheduled_date'] < today and task.row['status'] != 'completed':
            raise PendingObligations('기한이 지난 보충·복습을 먼저 이월한 뒤 과목을 추가해 주세요')
        is_fixed = not task.paged or task.row['task_type'] in ('remedial', 'review') or any(
            session['status'] in ('running', 'paused') or (session['status'] in ('ended', 'auto_stopped')
                and session['id'] not in consumed_sessions) for session in sessions[task.id]) or task.row['status'] == 'completed' or (
            task.record is not None and task.row['scheduled_date'] >= today)
        active_parts = []
        for span in task.paged:
            unit = catalog.get(span['unit_id'])
            if unit is None:
                if task.row['task_type'] != 'study':
                    raise PlanInputError('보충·복습의 과거 단원 범위를 먼저 확인해 주세요')
                continue
            actual = PageSpan(unit.material_id, span['start_page'], span['end_page'])
            if is_fixed:
                active_parts.append(FixedRange(unit.id, actual, task.id if task.row['task_type'] != 'study' else None))
        if is_fixed:
            pending_fixed.append((task, tuple(active_parts)))
        else:
            supersede.append(task.id)
    zone = ZoneInfo(timezone_name)
    for task, parts in sorted(pending_fixed, key=lambda item: (item[0].row['scheduled_date'], item[0].row['starts_at'] is None, item[0].id)):
        row, day = task.row, task.row['scheduled_date']
        if row['starts_at'] is not None and row['ends_at'] is not None:
            start = row['starts_at'].replace(tzinfo=timezone.utc) if row['starts_at'].tzinfo is None else row['starts_at']
            end = row['ends_at'].replace(tzinfo=timezone.utc) if row['ends_at'].tzinfo is None else row['ends_at']
            start, end = start.astimezone(zone), end.astimezone(zone)
            if start.date() != day or end.date() != day:
                raise PlanInputError('시작한 태스크의 날짜와 시간 범위를 확인해 주세요')
            first = Fraction(start.hour * 3600 + start.minute * 60 + start.second, 60) + Fraction(start.microsecond, 60_000_000)
            last = Fraction(end.hour * 3600 + end.minute * 60 + end.second, 60) + Fraction(end.microsecond, 60_000_000)
        else:
            # Old placements contain only a date. Reserve their real planned
            # duration inside a free interval; never invent persisted timestamps.
            duration = Fraction(row['estimated_minutes'])
            slot = next((slot for slot in free_slots(day, request.busy, fixed) if slot.minutes >= duration), None)
            if slot is None or duration <= 0:
                raise PlanInputError('기존 태스크를 보존할 연속 시간이 부족해요')
            first, last = slot.start, slot.start + duration
        try:
            fixed.append(FixedTask(task.id, row['subject_id'], row['task_type'],
                'completed' if row['status'] == 'completed' else 'started', day, first, last, parts))
        except ValueError as exc:
            raise PlanInputError('기존 배치의 날짜와 분량을 확인해 주세요') from exc
    units = tuple(replace(unit, ranges=subtract(unit.ranges, learned[unit.id])) for unit in request.units)
    return replace(request, units=units, fixed=tuple(fixed)), supersede, request
