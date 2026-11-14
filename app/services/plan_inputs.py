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
    free_slots, WorkRequirement, ScopePart)
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


def _allocation_input(connection, user_id, *, today, timezone_name, subject_ids, existing, material_structure_overrides=None,
                      subject_overrides=None, calendar_event_overrides=None, calendar_exception_overrides=None):
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
        if not isinstance(subject_ids, tuple) or (not subject_ids and not existing) or any(type(item) is not str for item in subject_ids):
            raise PlanInputError("선택할 draft 과목 ID 목록이 필요해요")
        try:
            selected = tuple(str(UUID(item)) for item in subject_ids)
        except ValueError as exc:
            raise PlanInputError("올바른 과목 UUID가 필요해요") from exc
        if len(set(selected)) != len(selected) or not set(selected) <= {row["id"] for row in rows}:
            raise PlanInputError("중복 없이 본인의 draft 과목을 선택해 주세요")
        rows = [row for row in rows if row["id"] in selected]
    if not rows and not existing:
        raise PlanInputError("계획할 draft 과목이 필요해요")
    if existing:
        rows += [dict(row) for row in connection.execute(select(t.subjects).where(
            t.subjects.c.user_id == user_id, t.subjects.c.status == 'active').order_by(t.subjects.c.id)).mappings()]
    overrides = {} if subject_overrides is None else subject_overrides
    if not isinstance(overrides, dict) or not set(overrides) <= {row['id'] for row in rows}:
        raise PlanInputError('본인의 선택된 과목만 변경할 수 있어요')
    for row in rows:
        values = overrides.get(row['id'], {})
        if not isinstance(values, dict) or not set(values) <= {'exam_date', 'target_grade', 'target_score', 'minutes_per_page'}:
            raise PlanInputError('시험일·목표·기준 속도만 변경할 수 있어요')
        if 'exam_date' in values and (type(values['exam_date']) is not date or values['exam_date'] <= today):
            raise PlanInputError('시험일은 오늘보다 뒤여야 해요')
        if 'target_grade' in values and (type(values['target_grade']) is not str or not values['target_grade'].strip()):
            raise PlanInputError('목표 학점을 확인해 주세요')
        row.update(values)
    if existing:
        rows = [row for row in rows if row['status'] == 'draft' or
                (row['exam_date'] is not None and row['exam_date'] > today)]
    if not rows:
        raise PlanInputError('재배분할 유효한 과목이 필요해요')
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
    concepts = defaultdict(list)
    for row in connection.execute(select(t.concepts).where(
            t.concepts.c.unit_id.in_([unit["id"] for unit in unit_rows]), t.concepts.c.retired_at.is_(None)
    ).order_by(t.concepts.c.unit_id, t.concepts.c.sort_order, t.concepts.c.id)).mappings():
        concepts[row["unit_id"]].append(dict(row))
    overrides = {} if material_structure_overrides is None else material_structure_overrides
    if not isinstance(overrides, dict) or not set(overrides) <= set(by_material):
        raise PlanInputError('본인 과목의 자료 구조만 변경할 수 있어요')
    all_unit_ids, all_concept_ids = set(), set()
    for material_id, structure in overrides.items():
        if not isinstance(structure, dict) or set(structure) != {'units', 'concepts'}:
            raise PlanInputError('후보 단원과 개념 구조가 필요해요')
        candidate_units, candidate_concepts = structure['units'], structure['concepts']
        if not isinstance(candidate_units, list) or not candidate_units or not isinstance(candidate_concepts, list):
            raise PlanInputError('빈 자료 구조로 계획을 만들 수 없어요')
        replacement = []
        identifiers = set()
        for row in candidate_units:
            if not isinstance(row, dict):
                raise PlanInputError('단원 후보가 올바르지 않아요')
            try:
                identifier = str(UUID(row['id']))
            except (KeyError, ValueError, TypeError, AttributeError):
                raise PlanInputError('단원 후보 UUID가 올바르지 않아요') from None
            if not {'sort_order', 'start_page', 'end_page', 'in_scope'} <= set(row) or type(row['in_scope']) is not bool:
                raise PlanInputError('단원 후보의 범위·순서·시험 선택이 필요해요')
            if identifier in all_unit_ids or row.get('material_id') != material_id:
                raise PlanInputError('단원 후보가 중복되거나 자료 소속이 달라요')
            current_material = connection.scalar(select(t.units.c.material_id).where(t.units.c.id == identifier))
            if current_material is not None and current_material != material_id:
                raise PlanInputError('다른 자료의 단원 ID를 사용할 수 없어요')
            identifiers.add(identifier)
            all_unit_ids.add(identifier)
            replacement.append({'quantity_basis': 'page', 'difficulty': None, 'mastery_score': None,
                                **row, 'id': identifier, 'retired_at': None})
        unit_rows = [row for row in unit_rows if row['material_id'] != material_id] + replacement
        for identifier in identifiers:
            concepts[identifier] = []
        for row in candidate_concepts:
            if not isinstance(row, dict) or row.get('unit_id') not in identifiers:
                raise PlanInputError('개념 후보의 단원 소속이 달라요')
            try:
                identifier = str(UUID(row['id']))
            except (KeyError, ValueError, TypeError, AttributeError):
                raise PlanInputError('개념 후보 UUID가 올바르지 않아요') from None
            if not {'start_page', 'end_page', 'in_scope'} <= set(row) or type(row['in_scope']) is not bool:
                raise PlanInputError('개념 후보의 범위와 시험 선택이 필요해요')
            if identifier in all_concept_ids:
                raise PlanInputError('개념 후보가 중복돼요')
            all_concept_ids.add(identifier)
            current_unit = connection.scalar(select(t.concepts.c.unit_id).where(t.concepts.c.id == identifier))
            if current_unit is not None and current_unit != row['unit_id']:
                raise PlanInputError('다른 단원의 개념 ID를 사용할 수 없어요')
            concepts[row['unit_id']].append({**row, 'id': identifier})
    if {row["material_id"] for row in unit_rows} != set(by_material):
        raise PlanInputError("자료별 페이지 목차를 확인해 주세요")
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
    event_changes = {} if calendar_event_overrides is None else calendar_event_overrides
    exception_changes = {} if calendar_exception_overrides is None else calendar_exception_overrides
    if not isinstance(event_changes, dict) or not isinstance(exception_changes, dict):
        raise PlanInputError('일정 후보는 ID별 변경 객체여야 해요')
    by_event = {event['id']: event for event in events}
    owned_subjects = set(connection.scalars(select(t.subjects.c.id).where(t.subjects.c.user_id == user_id)))
    for identifier, candidate in event_changes.items():
        try:
            if str(UUID(identifier)) != identifier:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PlanInputError('올바른 일정 UUID가 필요해요') from None
        current = connection.execute(select(t.calendar_events.c.user_id, t.calendar_events.c.subject_id)
            .where(t.calendar_events.c.id == identifier)).mappings().first()
        if current and (current['user_id'] != user_id or current['subject_id'] is not None
                        and current['subject_id'] not in owned_subjects):
            raise PlanInputError('다른 사용자의 일정을 변경할 수 없어요')
        if candidate is None:
            if identifier not in by_event:
                raise PlanInputError('제거할 본인의 일정이 없어요')
            del by_event[identifier]
            continue
        if (not isinstance(candidate, dict) or candidate.get('id') != identifier or candidate.get('user_id') != user_id
                or candidate.get('subject_id') is not None and candidate['subject_id'] not in owned_subjects
                or not {'starts_at', 'ends_at', 'recurrence_rule', 'recurrence_until'} <= set(candidate)
                or candidate.get('event_type') not in ('class', 'job', 'meet', 'other')):
            raise PlanInputError('본인 일정의 올바른 후보가 필요해요')
        if candidate.get('deleted_at') is not None:
            by_event.pop(identifier, None)
        else:
            by_event[identifier] = dict(candidate)
    for identifier, rows in exception_changes.items():
        if identifier not in by_event or not isinstance(rows, list):
            raise PlanInputError('유효한 본인 일정의 회차 변경만 지정해 주세요')
        if any(not isinstance(row, dict) or row.get('event_id') != identifier for row in rows):
            raise PlanInputError('회차 변경의 소속 일정이 달라요')
        exceptions[identifier] = [dict(row) for row in rows]
    events = list(by_event.values())
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


def existing_allocation_input(connection, user_id, *, today, timezone_name, subject_ids,
                              material_structure_overrides=None, affected_subject_ids=None,
                              subject_overrides=None, earliest_day=None, movable_task_ids=(),
                              calendar_event_overrides=None, calendar_exception_overrides=None):
    """Keep learning facts and active started placements while reallocating rest.

    Only selected draft subjects are added. Every active study range is reduced
    by actual recorded pages; remedial/review ranges remain separate obligations.
    Superseded rows are retained (including records, quiz/book references).
    """
    if (not subject_ids and material_structure_overrides is None and subject_overrides is None
            and affected_subject_ids is None and calendar_event_overrides is None
            and calendar_exception_overrides is None and not movable_task_ids):
        raise PlanInputError('재배분할 과목이나 변경할 입력을 선택해 주세요')
    earliest_day = today if earliest_day is None else earliest_day
    if type(earliest_day) is not date or earliest_day < today:
        raise PlanInputError('재배분 시작일은 오늘 이후여야 해요')
    request = _allocation_input(connection, user_id, today=today, timezone_name=timezone_name,
                                subject_ids=subject_ids, existing=True, material_structure_overrides=material_structure_overrides,
                                subject_overrides=subject_overrides, calendar_event_overrides=calendar_event_overrides,
                                calendar_exception_overrides=calendar_exception_overrides)
    from app.services.study import StudyData
    data = StudyData(connection, user_id)
    included = {subject.id for subject in request.subjects}
    catalog = {unit.id: unit for unit in request.units}
    affected = included if affected_subject_ids is None else set(affected_subject_ids)
    if not affected or not affected <= included:
        raise PlanInputError('재배분할 과목 소속이 올바르지 않아요')
    historical = {row['id']: dict(row) for row in connection.execute(select(t.units, t.materials.c.subject_id)
        .join(t.materials, t.materials.c.id == t.units.c.material_id).where(t.materials.c.subject_id.in_(included))).mappings()}
    # Fixed placements keep their historical unit identity even when the current
    # structure retires it. Empty scopes add no new learning obligations.
    for identifier, row in historical.items():
        if identifier not in catalog:
            catalog[identifier] = UnitScope(identifier, row['subject_id'], row['material_id'], row['sort_order'],
                                            (), row['difficulty'], row['mastery_score'])
    sessions = defaultdict(list)
    for row in connection.execute(select(t.study_sessions).where(t.study_sessions.c.user_id == user_id)).mappings():
        sessions[row['task_id']].append(dict(row))
    consumed_sessions = set(connection.scalars(select(t.study_records.c.session_id).where(
        t.study_records.c.task_id.in_([task.id for task in data.tasks]), t.study_records.c.session_id.is_not(None))))
    if not isinstance(movable_task_ids, (tuple, list)) or any(type(value) is not str for value in movable_task_ids):
        raise PlanInputError('이동할 태스크 ID 목록이 필요해요')
    movable = set(movable_task_ids)
    if len(movable) != len(movable_task_ids) or not movable <= {task.id for task in data.tasks if task.row['subject_id'] in affected}:
        raise PlanInputError('본인의 재배분 대상 태스크만 중복 없이 이동할 수 있어요')
    learned, fixed, supersede, requirements = defaultdict(list), [], [], []
    pending_fixed = []
    for task in data.tasks:
        if task.row['subject_id'] not in included:
            continue
        if task.record and task.record['result'] in ('completed', 'partial'):
            for span in task.done_ranges:
                if span['unit_id'] in catalog and span['start_page'] is not None and span['end_page'] is not None:
                    unit = catalog[span['unit_id']]
                    if task.row['task_type'] == 'study':
                        learned[unit.material_id].append(PageSpan(unit.material_id, span['start_page'], span['end_page']))
        if task.row['status'] in ('carried_over', 'excluded'):
            continue
        if task.id not in movable and task.row['subject_id'] in affected and task.row['task_type'] in ('remedial', 'review') and task.row['scheduled_date'] < today and task.row['status'] != 'completed':
            raise PendingObligations('기한이 지난 보충·복습을 먼저 이월한 뒤 과목을 추가해 주세요')
        protected_session = any(session['status'] in ('running', 'paused') or (
            session['status'] in ('ended', 'auto_stopped') and session['id'] not in consumed_sessions)
            for session in sessions[task.id])
        is_fixed = (task.row['subject_id'] not in affected or not task.paged or protected_session
            or task.row['status'] == 'completed' or (task.id not in movable and (
                task.row['task_type'] in ('remedial', 'review') or task.row['scheduled_date'] < earliest_day
                and task.row['scheduled_date'] >= today or task.record is not None and task.row['scheduled_date'] >= today)))
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
            if task.row['task_type'] in ('remedial', 'review'):
                remaining = []
                for span in task.paged:
                    unit = catalog[span['unit_id']]
                    done = [PageSpan(unit.material_id, row['start_page'], row['end_page']) for row in task.done_ranges
                        if row['unit_id'] == unit.id and row['start_page'] is not None and row['end_page'] is not None]
                    remaining.extend(ScopePart(unit.id, part) for part in subtract(
                        (PageSpan(unit.material_id, span['start_page'], span['end_page']),), done))
                if remaining:
                    requirements.append(WorkRequirement(task.id, task.row['subject_id'], task.row['task_type'],
                        tuple(remaining), earliest_day))
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
    units = tuple(replace(unit, ranges=subtract(unit.ranges, learned[unit.material_id]) if unit.subject_id in affected else ())
                  for unit in catalog.values())
    return replace(request, start_date=earliest_day, units=units, fixed=tuple(fixed),
                   requirements=tuple(requirements)), supersede, request
