"""Create immutable learning-pass evidence while the record's user lock is held."""
from uuid import uuid4

from sqlalchemy import func, select

from app.ai.metrics_adapter import current_scope
from app.db import schema as t
from app.services.learning_evidence import _covers, _required_pages
from app.services.recovery_evidence import first_passes
from app.services.study import StudyData


def scope_revision(conn, unit):
    material = conn.execute(select(t.materials.c.sha256, t.materials.c.analysis_version).where(
        t.materials.c.id == unit["material_id"])).mappings().one()
    concepts = conn.execute(select(t.concepts).where(t.concepts.c.unit_id == unit["id"])).mappings().all()
    return current_scope({**unit, "material_sha256": material["sha256"],
                          "analysis_version": material["analysis_version"]}, concepts).revision


def consumed_evidence(conn, unit_id):
    """Invalidation reserves the original proof for restoration of that pass."""
    record_ids, session_ids = set(), set()
    for snapshot in conn.scalars(select(t.learning_passes.c.evidence_snapshot).where(
            t.learning_passes.c.unit_id == unit_id)):
        record_ids.update(snapshot.get("recordIds", []))
        session_ids.update(snapshot.get("sessionIds", []))
    session_ids.update(conn.scalars(select(t.study_records.c.session_id).where(
        t.study_records.c.id.in_(record_ids), t.study_records.c.session_id.is_not(None))))
    return record_ids, session_ids


def create_initial_passes(conn, user_id, task, record_id, at):
    """Only actual study pages can establish the unit's first completion.

    Retired units and unpaged tasks have no current page requirement. Existing
    initial evidence, including invalidated evidence, is never replaced.
    """
    if task.row["task_type"] != "study":
        return
    data = StudyData(conn, user_id)
    for unit_id in sorted({row["unit_id"] for row in task.ranges}):
        required = [(span.start, span.end) for span in data.unit_scopes.get(unit_id, ())]
        if not required or conn.scalar(select(t.learning_passes.c.id).where(
                t.learning_passes.c.unit_id == unit_id,
                t.learning_passes.c.completion_kind == "initial")):
            continue
        unit = data.units[unit_id]
        consumed_records, consumed_sessions = consumed_evidence(conn, unit_id)
        evidence = conn.execute(select(
            t.study_records.c.id, t.study_records.c.session_id, t.study_records.c.study_date,
            t.study_record_ranges.c.start_page, t.study_record_ranges.c.end_page)
            .join(t.study_record_ranges, t.study_record_ranges.c.record_id == t.study_records.c.id)
            .join(t.study_tasks, t.study_tasks.c.id == t.study_records.c.task_id)
            .join(t.subjects, t.subjects.c.id == t.study_tasks.c.subject_id)
            .where(t.subjects.c.user_id == user_id, t.study_tasks.c.subject_id == unit["subject_id"],
                   t.study_tasks.c.task_type == "study", t.study_record_ranges.c.unit_id == unit_id,
                   t.study_records.c.result.in_(("completed", "partial")),
                   t.study_record_ranges.c.start_page.is_not(None),
                   t.study_record_ranges.c.end_page.is_not(None))
            .order_by(t.study_records.c.study_date, t.study_records.c.recorded_at, t.study_records.c.id)
        ).mappings().all()
        evidence = [row for row in evidence if row["id"] not in consumed_records
                    and row["session_id"] not in consumed_sessions and any(
                        row["start_page"] <= end and start <= row["end_page"] for start, end in required)]
        if record_id not in {row["id"] for row in evidence} or not _covers(
                required, [(row["start_page"], row["end_page"]) for row in evidence]):
            continue
        number = 1 + (conn.scalar(select(func.max(t.learning_passes.c.pass_number)).where(
            t.learning_passes.c.unit_id == unit_id)) or 0)
        conn.execute(t.learning_passes.insert().values(
            id=str(uuid4()), unit_id=unit_id, pass_number=number, completion_kind="initial",
            source_task_id=task.id, evidence_key="initial",
            evidence_snapshot={"schemaVersion": "1", "materialId": unit["material_id"],
                               "masteryScopeRevision": scope_revision(conn, unit),
                               "recordIds": list(dict.fromkeys(row["id"] for row in evidence)),
                               "sessionIds": sorted({row["session_id"] for row in evidence if row["session_id"]}),
                               "requiredRanges": [list(pair) for pair in required]},
            completed_at=at, completed_date=max(row["study_date"] for row in evidence)))


def create_remedial_passes(conn, user_id, task, record_id, at):
    """One obligation may span many books; its fixed union grants one pass."""
    if task.row["task_type"] != "remedial":
        return
    obligations = conn.execute(select(t.remedial_obligations)
        .join(t.remedial_obligation_blocks, t.remedial_obligation_blocks.c.obligation_id == t.remedial_obligations.c.id)
        .join(t.quiz_sessions, t.quiz_sessions.c.id == t.remedial_obligations.c.quiz_session_id)
        .where(t.remedial_obligation_blocks.c.block_id == task.row["block_id"],
               t.remedial_obligations.c.user_id == user_id, t.quiz_sessions.c.user_id == user_id,
               t.quiz_sessions.c.status == "submitted", t.quiz_sessions.c.finalized_at.is_not(None),
               t.quiz_sessions.c.grading_status == "completed")).mappings().all()
    for obligation in obligations:
        unit_id = obligation["unit_id"]
        if conn.scalar(select(t.learning_passes.c.id).where(
                t.learning_passes.c.unit_id == unit_id, t.learning_passes.c.completion_kind == "remedial",
                t.learning_passes.c.evidence_key == obligation["id"])):
            continue
        unit = conn.execute(select(t.units, t.materials.c.subject_id,
                                   t.materials.c.sha256.label("material_sha256"), t.materials.c.analysis_version)
            .join(t.materials, t.materials.c.id == t.units.c.material_id)
            .join(t.subjects, t.subjects.c.id == t.materials.c.subject_id)
            .where(t.units.c.id == unit_id, t.subjects.c.user_id == user_id,
                   t.units.c.quantity_basis == "page")).mappings().first()
        if unit is None:
            continue
        concepts = conn.execute(select(t.concepts).where(t.concepts.c.unit_id == unit_id)).mappings().all()
        first = first_passes(conn, user_id, unit, concepts)
        if not first:
            continue
        block_ids = set(conn.scalars(select(t.remedial_obligation_blocks.c.block_id).where(
            t.remedial_obligation_blocks.c.obligation_id == obligation["id"])))
        owned = set(conn.scalars(select(t.study_blocks.c.id).where(
            t.study_blocks.c.id.in_(block_ids), t.study_blocks.c.subject_id == unit["subject_id"],
            t.study_blocks.c.unit_id == unit_id, t.study_blocks.c.task_type == "remedial")))
        if owned != block_ids:
            continue
        required = _required_pages({"schemaVersion": "1", "requiredRanges": obligation["required_ranges"]})
        consumed_records, consumed_sessions = consumed_evidence(conn, unit_id)
        evidence = conn.execute(select(
            t.study_records.c.id, t.study_records.c.session_id, t.study_records.c.study_date,
            t.study_record_ranges.c.start_page, t.study_record_ranges.c.end_page)
            .join(t.study_record_ranges, t.study_record_ranges.c.record_id == t.study_records.c.id)
            .join(t.study_tasks, t.study_tasks.c.id == t.study_records.c.task_id)
            .where(t.study_tasks.c.block_id.in_(block_ids), t.study_tasks.c.subject_id == unit["subject_id"],
                   t.study_tasks.c.task_type == "remedial", t.study_record_ranges.c.unit_id == unit_id,
                   t.study_records.c.result.in_(("completed", "partial")),
                   t.study_records.c.study_date >= obligation["not_before"],
                   t.study_record_ranges.c.start_page.is_not(None),
                   t.study_record_ranges.c.end_page.is_not(None))
            .order_by(t.study_records.c.study_date, t.study_records.c.recorded_at, t.study_records.c.id)
        ).mappings().all()
        evidence = [row for row in evidence if row["id"] not in consumed_records
                    and row["session_id"] not in consumed_sessions and any(
                        row["start_page"] <= end and start <= row["end_page"] for start, end in required)]
        if record_id not in {row["id"] for row in evidence} or not _covers(
                required, [(row["start_page"], row["end_page"]) for row in evidence]):
            continue
        completed_day = max(row["study_date"] for row in evidence)
        if not any(row["completed_date"] <= completed_day for row in first):
            continue
        number = 1 + (conn.scalar(select(func.max(t.learning_passes.c.pass_number)).where(
            t.learning_passes.c.unit_id == unit_id)) or 0)
        conn.execute(t.learning_passes.insert().values(
            id=str(uuid4()), unit_id=unit_id, pass_number=number, completion_kind="remedial",
            source_task_id=task.id, evidence_key=obligation["id"],
            evidence_snapshot={"schemaVersion": "1", "materialId": unit["material_id"],
                "obligationId": obligation["id"], "blockIds": sorted(block_ids),
                "sourceQuizId": obligation["quiz_session_id"],
                "masteryScopeRevision": obligation["scope_revision"],
                "recordIds": list(dict.fromkeys(row["id"] for row in evidence)),
                "sessionIds": sorted({row["session_id"] for row in evidence if row["session_id"]}),
                "requiredRanges": [list(pair) for pair in required]},
            completed_at=at, completed_date=completed_day))
