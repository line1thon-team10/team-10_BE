"""Initial plan preparation against migrated SQLite/PostgreSQL rows."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.onboarding import CONFIRMATION_COLUMNS
from app.domain.ranges import page_count
from app.domain.scheduling import free_slots
from app.services.allocator import allocate
from app.services.plan_inputs import PlanInputError, initial_allocation_input

TODAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


@pytest.fixture
def plan_case(domain_connection):
    conn = domain_connection
    ids = {key: str(uuid4()) for key in ("user", "other_user", "subject", "active", "foreign", "material", "unit")}
    for key in ("user", "other_user"):
        conn.execute(t.users.insert().values(id=ids[key], demo_date=TODAY, created_at=NOW, updated_at=NOW))
    for key, owner, status in (("subject", "user", "draft"), ("active", "user", "active"), ("foreign", "other_user", "draft")):
        conn.execute(t.subjects.insert().values(id=ids[key], user_id=ids[owner], code=key, name=key, color="#123456",
            status=status, exam_date=TODAY + timedelta(days=3), target_grade="A+", target_score=90,
            **{column: NOW for column in CONFIRMATION_COLUMNS.values()}, created_at=NOW, updated_at=NOW))
    conn.execute(t.materials.insert().values(id=ids["material"], subject_id=ids["subject"], original_name="manual.pdf",
        source_format="pdf", sha256="manual", page_count=20, analysis_version="test", analysis_status="failed",
        is_fallback=True, quiz_enabled=False, created_at=NOW, updated_at=NOW))
    conn.execute(t.units.insert().values(id=ids["unit"], material_id=ids["material"], code="unit1", title="수동 목차",
        sort_order=1, start_page=1, end_page=20, difficulty="low", in_scope=True, created_at=NOW, updated_at=NOW))
    conn.commit()
    return ids


def prepare(conn, case, **options):
    return initial_allocation_input(conn, case["user"], today=TODAY, timezone_name="Asia/Seoul", **options)


def concept(conn, unit, start, end, *, sort_order=1, **values):
    identifier = str(uuid4())
    conn.execute(t.concepts.insert().values(id=identifier, unit_id=unit, code=identifier, title="개념", sort_order=sort_order,
        start_page=start, end_page=end, created_at=NOW, updated_at=NOW, **values))
    return identifier


def test_manual_quizless_pages_allocate_without_mutating_active_or_creating_plan(domain_connection, plan_case):
    conn, case = domain_connection, plan_case
    before = [dict(row) for row in conn.execute(select(t.subjects).order_by(t.subjects.c.id)).mappings()]
    request = prepare(conn, case)
    assert [subject.id for subject in request.subjects] == [case["subject"]]
    assert [unit.id for unit in request.units] == [case["unit"]]
    assert page_count(span for unit in request.units for span in unit.ranges) == 20
    result = allocate(request)
    assert result.feasible and not result.unplaced
    assert sum(part.span.end - part.span.start + 1 for task in result.placements for part in task.parts) == 20
    assert all(15 <= task.minutes <= 60 and TODAY <= task.day < TODAY + timedelta(days=3) for task in result.placements)
    assert conn.execute(select(t.plans)).first() is None and conn.execute(select(t.study_tasks)).first() is None
    assert [dict(row) for row in conn.execute(select(t.subjects).order_by(t.subjects.c.id)).mappings()] == before


def test_concept_union_and_different_pdf_identity_are_preserved(domain_connection, plan_case):
    conn, case = domain_connection, plan_case
    concept(conn, case["unit"], 1, 8)
    concept(conn, case["unit"], 5, 12, sort_order=2)
    concept(conn, case["unit"], 13, 20, sort_order=3, in_scope=False)
    concept(conn, case["unit"], 1, 20, retired_at=NOW)
    material = dict(conn.execute(select(t.materials).where(t.materials.c.id == case["material"])).mappings().one())
    unit = dict(conn.execute(select(t.units).where(t.units.c.id == case["unit"])).mappings().one())
    other_material, other_unit = str(uuid4()), str(uuid4())
    conn.execute(t.materials.insert().values(**{**material, "id": other_material, "sha256": "second"}))
    conn.execute(t.units.insert().values(**{**unit, "id": other_unit, "material_id": other_material, "end_page": 6}))
    request = prepare(conn, case)
    assert page_count(span for scope in request.units for span in scope.ranges) == 18
    scopes = {scope.id: scope for scope in request.units}
    assert [(span.start, span.end) for span in scopes[case["unit"]].ranges] == [(1, 12)]
    assert scopes[other_unit].material_id == other_material


@pytest.mark.parametrize("field", list(CONFIRMATION_COLUMNS.values()) + ["target_score", "exam_date", "target_grade"])
def test_missing_confirmation_or_required_goal_never_prepares(domain_connection, plan_case, field):
    conn, case = domain_connection, plan_case
    conn.execute(update(t.subjects).where(t.subjects.c.id == case["subject"]).values(**{field: None}))
    with pytest.raises(PlanInputError):
        prepare(conn, case)


@pytest.mark.parametrize("kind", ["foreign", "active", "missing", "duplicate", "empty"])
def test_explicit_selection_cannot_include_foreign_active_missing_or_duplicate_ids(domain_connection, plan_case, kind):
    case = plan_case
    identifiers = {"foreign": (case["foreign"],), "active": (case["active"],), "missing": (str(uuid4()),),
                   "duplicate": (case["subject"], case["subject"].upper().replace("-", "")), "empty": ()}[kind]
    with pytest.raises(PlanInputError):
        prepare(domain_connection, case, subject_ids=identifiers)


@pytest.mark.parametrize("kind", ["past_exam", "unknown_pages", "outside_pdf", "time_only", "outside_unit", "unit_overlap", "all_concepts_excluded"])
def test_invalid_or_empty_page_structure_is_rejected(domain_connection, plan_case, kind):
    conn, case = domain_connection, plan_case
    if kind == "past_exam":
        conn.execute(update(t.subjects).where(t.subjects.c.id == case["subject"]).values(exam_date=TODAY))
    elif kind == "unknown_pages":
        conn.execute(update(t.materials).where(t.materials.c.id == case["material"]).values(page_count=None))
    elif kind in ("outside_pdf", "time_only"):
        values = {"end_page": 21} if kind == "outside_pdf" else {"quantity_basis": "time", "start_page": None, "end_page": None}
        conn.execute(update(t.units).where(t.units.c.id == case["unit"]).values(**values))
    elif kind == "outside_unit":
        concept(conn, case["unit"], 1, 21)
    elif kind == "unit_overlap":
        row = dict(conn.execute(select(t.units).where(t.units.c.id == case["unit"])).mappings().one())
        conn.execute(t.units.insert().values(**{**row, "id": str(uuid4()), "code": "unit2", "sort_order": 2, "start_page": 20}))
    else:
        concept(conn, case["unit"], 1, 20, in_scope=False)
    with pytest.raises(PlanInputError, match="같은 PDF의 단원 범위가 겹쳐요" if kind == "unit_overlap" else None):
        prepare(conn, case)


def test_recurrence_exceptions_and_busy_union_reach_allocator(domain_connection, plan_case):
    conn, case = domain_connection, plan_case
    event_id = str(uuid4())
    # 08:00-09:00 KST daily; Tuesday is cancelled and Wednesday is moved to 10:00.
    conn.execute(t.calendar_events.insert().values(id=event_id, user_id=case["user"], event_type="class", title="수업",
        starts_at=NOW - timedelta(hours=1), ends_at=NOW, recurrence_rule="FREQ=WEEKLY;BYDAY=MO,TU,WE",
        created_at=NOW, updated_at=NOW))
    for offset, kind in ((1, "cancelled"), (2, "moved")):
        conn.execute(t.calendar_event_exceptions.insert().values(id=str(uuid4()), event_id=event_id,
            occurrence_date=TODAY + timedelta(days=offset), kind=kind,
            new_starts_at=NOW + timedelta(days=2, hours=1) if kind == "moved" else None,
            new_ends_at=NOW + timedelta(days=2, hours=2) if kind == "moved" else None,
            created_at=NOW, updated_at=NOW))
    request = prepare(conn, case)
    assert [(item.day, item.start, item.end) for item in request.busy] == [
        (TODAY, 480, 540), (TODAY + timedelta(days=2), 600, 660)]
    assert sum(slot.minutes for slot in free_slots(TODAY + timedelta(days=1), request.busy)) == 900
