"""A split/carryable remedial obligation consumes real evidence exactly once."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.services.learning_passes import scope_revision
from tests.test_initial_pass import passes, placement, record
from tests.test_record_transactions import headers, task_for


def obligation(conn, user, template, task_ids, *, required=None, not_before=None):
    unit_id = template.ranges[0]["unit_id"]
    unit = conn.execute(select(t.units).where(t.units.c.id == unit_id)).mappings().one()
    qid, oid = str(uuid4()), str(uuid4())
    conn.execute(t.quiz_sessions.insert().values(
        id=qid, user_id=user, subject_id=template.row["subject_id"], type="daily",
        quiz_date=template.row["scheduled_date"] - timedelta(days=1), status="submitted",
        grading_status="completed", created_at=template.row["created_at"], finalized_at=template.row["created_at"]))
    conn.execute(t.remedial_obligations.insert().values(
        id=oid, user_id=user, unit_id=unit_id, quiz_session_id=qid,
        scope_revision=scope_revision(conn, unit), required_ranges=required or [[12, 20]],
        not_before=not_before or template.row["scheduled_date"], created_at=template.row["created_at"]))
    for block_id in set(conn.scalars(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id.in_(task_ids)))):
        conn.execute(t.remedial_obligation_blocks.insert().values(obligation_id=oid, block_id=block_id))
    return oid


def remedials(conn, unit_id):
    return [row for row in passes(conn, unit_id) if row["completion_kind"] == "remedial"]


def test_split_obligation_carry_and_correction_preserve_one_pass(api_client, demo_user, domain_connection):
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    first = placement(conn, template, 12, 15, task_type="remedial")
    second = placement(conn, template, 16, 20, task_type="remedial")
    oid = obligation(conn, demo_user, template, [first, second])
    conn.commit()
    record(api_client, demo_user, first, result="partial", toPage=14)
    record(api_client, demo_user, second)
    assert remedials(conn, uid) == []
    first_block = conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id == first))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == first).values(status="carried_over"))
    carried = placement(conn, template, 15, 15, task_type="remedial", offset=1, block_id=first_block, origin=first)
    next_day = template.row["scheduled_date"] + timedelta(days=1)
    conn.execute(update(t.users).where(t.users.c.id == demo_user).values(demo_date=next_day))
    conn.commit()
    record(api_client, demo_user, carried)
    before = remedials(conn, uid)
    assert len(before) == 1 and before[0]["pass_number"] == 2
    assert before[0]["evidence_key"] == oid
    assert before[0]["evidence_snapshot"]["obligationId"] == oid
    assert len(before[0]["evidence_snapshot"]["blockIds"]) == 2
    assert len(before[0]["evidence_snapshot"]["recordIds"]) == 3
    assert before[0]["completed_date"] == next_day
    conn.commit()
    record(api_client, demo_user, carried)
    assert remedials(conn, uid) == before
    conn.commit()
    response = api_client.patch(f"/api/tasks/{first}/record", headers=headers(demo_user), json={"toPage": 13})
    assert response.status_code == 200, response.text
    assert remedials(conn, uid)[0]["invalidated_at"] is not None
    conn.execute(update(t.users).where(t.users.c.id == demo_user).values(demo_date=next_day + timedelta(days=3)))
    conn.commit()
    response = api_client.patch(f"/api/tasks/{first}/record", headers=headers(demo_user), json={"toPage": 14})
    assert response.status_code == 200, response.text
    assert remedials(conn, uid) == before


@pytest.mark.parametrize("reason", ["no_obligation", "no_initial", "invalid_initial", "too_early", "unfinalized", "wrong_unit"])
def test_remedial_requires_owned_finalized_obligation_and_initial(api_client, demo_user, domain_connection, reason):
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    task_id = placement(conn, template, 12, 20, task_type="remedial")
    if reason != "no_obligation":
        oid = obligation(conn, demo_user, template, [task_id],
            not_before=template.row["scheduled_date"] + timedelta(days=1) if reason == "too_early" else None)
        if reason == "unfinalized":
            qid = conn.scalar(select(t.remedial_obligations.c.quiz_session_id).where(t.remedial_obligations.c.id == oid))
            conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == qid).values(finalized_at=None))
        if reason == "wrong_unit":
            wrong = task_for(conn, demo_user, "AI2").ranges[0]["unit_id"]
            block_id = conn.scalar(select(t.study_tasks.c.block_id).where(t.study_tasks.c.id == task_id))
            conn.execute(update(t.study_blocks).where(t.study_blocks.c.id == block_id).values(unit_id=wrong))
    if reason == "no_initial":
        conn.execute(t.learning_passes.delete().where(t.learning_passes.c.unit_id == uid))
    if reason == "invalid_initial":
        conn.execute(update(t.learning_passes).where(t.learning_passes.c.unit_id == uid).values(
            invalidated_at=template.row["created_at"]))
    conn.commit()
    record(api_client, demo_user, task_id)
    assert remedials(conn, uid) == []


def test_consumed_record_does_not_fund_another_pass(api_client, demo_user, domain_connection):
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    first = placement(conn, template, 12, 15, task_type="remedial")
    second = placement(conn, template, 16, 20, task_type="remedial")
    obligation(conn, demo_user, template, [first, second])
    conn.commit()
    record(api_client, demo_user, first)
    first_record = conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == first))
    initial = passes(conn, uid)[0]
    conn.execute(t.learning_passes.insert().values(**{
        **initial, "id": str(uuid4()), "pass_number": 2, "completion_kind": "full_relearn",
        "evidence_key": str(uuid4()), "evidence_snapshot": {
            "schemaVersion": "1", "recordIds": [first_record], "requiredRanges": [[12, 15]]}}))
    conn.commit()
    record(api_client, demo_user, second)
    assert remedials(conn, uid) == []


def test_retired_obligation_completes_fixed_historical_scope(api_client, demo_user, domain_connection):
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    task_id = placement(conn, template, 12, 20, task_type="remedial")
    oid = obligation(conn, demo_user, template, [task_id])
    revision = conn.scalar(select(t.remedial_obligations.c.scope_revision).where(t.remedial_obligations.c.id == oid))
    conn.execute(update(t.units).where(t.units.c.id == uid).values(retired_at=template.row["created_at"]))
    conn.commit()
    record(api_client, demo_user, task_id)
    result = remedials(conn, uid)
    assert len(result) == 1
    assert result[0]["evidence_snapshot"]["masteryScopeRevision"] == revision


def test_remedial_pass_failure_rolls_back_entire_record(api_client, demo_user, domain_connection, monkeypatch):
    from app.services import records
    conn = domain_connection
    template = task_for(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    task_id = placement(conn, template, 12, 20, task_type="remedial")
    obligation(conn, demo_user, template, [task_id])
    conn.commit()
    real = records.create_remedial_passes

    def fail(*args):
        real(*args)
        raise RuntimeError("after remedial pass")

    monkeypatch.setattr(records, "create_remedial_passes", fail)
    response = api_client.post("/api/records", headers=headers(demo_user), json={"taskId": task_id, "result": "done"})
    assert response.status_code == 500
    assert remedials(conn, uid) == []
    assert conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == task_id)) is None
    assert conn.scalar(select(t.study_tasks.c.status).where(t.study_tasks.c.id == task_id)) == "planned"


@pytest.mark.parametrize("proof", ["valid", "missing_records", "invalidated", "future"])
def test_full_relearning_first_can_qualify_without_an_initial_pass(api_client, demo_user, domain_connection, proof):
    from tests.test_planless_review_correction import direct_record
    conn = domain_connection
    template, _, _, full_id = direct_record(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    conn.execute(t.learning_passes.delete().where(
        t.learning_passes.c.unit_id == uid, t.learning_passes.c.completion_kind == "initial"))
    full = dict(conn.execute(select(t.learning_passes).where(t.learning_passes.c.id == full_id)).mappings().one())
    snapshot = {**full["evidence_snapshot"], "materialId": template.ranges[0]["material_id"],
                "directRequestHash": "a" * 64}
    if proof == "missing_records":
        snapshot["recordIds"] = []
        snapshot["directRequestHash"] = "a" * 64
    changes = {"evidence_snapshot": snapshot, "evidence_key": "direct:" + "b" * 64}
    if proof == "invalidated":
        changes["invalidated_at"] = full["completed_at"]
    if proof == "future":
        changes["completed_date"] = full["completed_date"] + timedelta(days=1)
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == full_id).values(**changes))
    remedial = placement(conn, template, 12, 20, task_type="remedial")
    obligation(conn, demo_user, template, [remedial])
    conn.commit()
    record(api_client, demo_user, remedial)
    result = remedials(conn, uid)
    assert len(result) == (1 if proof == "valid" else 0)
    if result:
        assert result[0]["pass_number"] == 3  # Historical numbering is not a valid-count field.
