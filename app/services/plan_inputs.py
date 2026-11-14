"""Read initial draft inputs for allocation-v1 without approving or writing.

The caller owns its transaction/user lock and captures the input versions before
calculating a preview. Existing-plan reallocation is a separate boundary.
"""
from collections import defaultdict
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select

from app.db import schema as t
from app.domain.onboarding import CONFIRMATION_COLUMNS, subject_view
from app.domain.ranges import PageSpan
from app.domain.scheduling import AllocationInput, Subject, UnitScope
from app.repositories.events import EventRepository
from app.services.onboarding_events import event_intervals


class PlanInputError(ValueError):
    """An internal preparation failure; the API owns public error mapping."""


def _page_bounds(start, end, maximum):
    if (type(start) is not int or type(end) is not int or type(maximum) is not int
            or not 1 <= start <= end <= maximum):
        raise PlanInputError("실제 PDF 안의 올바른 페이지 범위를 확인해 주세요")


def initial_allocation_input(connection, user_id: str, *, today: date,
                             timezone_name: str, subject_ids: tuple[str, ...] | None = None) -> AllocationInput:
    """Resolve owned draft subjects, concept unions and actual calendar intervals.

    quiz_enabled is deliberately not an admission condition: a manual outline
    without question evidence can still provide a valid page learning scope.
    This function never silently includes an active or foreign subject.
    """
    if type(today) is not date:
        raise PlanInputError("사용자 현지 날짜가 필요해요")
    if connection.scalar(select(t.plans.c.id).where(t.plans.c.user_id == user_id)) is not None:
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
    subjects = []
    for row in rows:
        if (not all(row[column] is not None for column in CONFIRMATION_COLUMNS.values())
                or subject_view(row)["status"] != "confirmed" or row["target_score"] is None):
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
