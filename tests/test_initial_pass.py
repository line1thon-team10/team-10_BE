"""HTTP record mutations create one fixed, unit-owned initial completion."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update

from app.db import schema as t
from tests.test_record_transactions import headers, task_for


def passes(conn, unit_id):
    return [dict(row) for row in conn.execute(select(t.learning_passes).where(
        t.learning_passes.c.unit_id == unit_id).order_by(t.learning_passes.c.pass_number)).mappings()]


def fresh_unit(conn, user):
    task = task_for(conn, user)
    unit_id = task.ranges[0]["unit_id"]
    conn.execute(delete(t.learning_passes).where(t.learning_passes.c.unit_id == unit_id))
    conn.execute(delete(t.study_record_ranges).where(t.study_record_ranges.c.unit_id == unit_id))
    conn.execute(update(t.concepts).where(t.concepts.c.unit_id == unit_id).values(retired_at=task.row["created_at"]))
    return task, unit_id


def placement(conn, template, start, end, *, task_type="study", offset=0, block_id=None, origin=None):
    task_id = str(uuid4())
    if block_id is None:
        block_id = str(uuid4())
        conn.execute(t.study_blocks.insert().values(
            id=block_id, subject_id=template.row["subject_id"], unit_id=template.ranges[0]["unit_id"],
            task_type=task_type, created_at=template.row["created_at"]))
    conn.execute(t.study_tasks.insert().values(**{
        **template.row, "id": task_id, "block_id": block_id, "origin_task_id": origin,
        "scheduled_date": template.row["scheduled_date"] + timedelta(days=offset),
        "task_type": task_type, "status": "planned"}))
    conn.execute(t.study_task_ranges.insert().values(
        id=str(uuid4()), task_id=task_id, unit_id=template.ranges[0]["unit_id"],
        start_page=start, end_page=end, sort_order=1))
    return task_id


def record(client, user, task_id, result="done", **extra):
    response = client.post("/api/records", headers=headers(user),
                           json={"taskId": task_id, "result": result, **extra})
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("first_end,last_start,complete", [(16, 16, True), (16, 18, False), (19, 20, True)])
def test_initial_uses_union_across_records_and_dates(
        api_client, demo_user, domain_connection, first_end, last_start, complete):
    conn = domain_connection
    template, unit_id = fresh_unit(conn, demo_user)
    first = placement(conn, template, 12, first_end)
    last = placement(conn, template, last_start, 20, offset=1)
    conn.commit()
    record(api_client, demo_user, first)
    assert passes(conn, unit_id) == []
    conn.execute(update(t.users).where(t.users.c.id == demo_user).values(
        demo_date=template.row["scheduled_date"] + timedelta(days=1)))
    conn.commit()
    record(api_client, demo_user, last)
    found = passes(conn, unit_id)
    assert len(found) == int(complete)
    if complete:
        item = found[0]
        assert item["pass_number"] == 1
        assert item["evidence_snapshot"]["requiredRanges"] == [[12, 20]]
        assert len(item["evidence_snapshot"]["recordIds"]) == 2
        assert item["completed_date"] == template.row["scheduled_date"] + timedelta(days=1)
        conn.commit()
        record(api_client, demo_user, last)
        assert passes(conn, unit_id) == found


def test_initial_correction_restores_exact_pass_and_original_date(api_client, demo_user, domain_connection):
    conn = domain_connection
    template, unit_id = fresh_unit(conn, demo_user)
    task_id = placement(conn, template, 12, 20)
    conn.commit()
    record(api_client, demo_user, task_id)
    before = passes(conn, unit_id)
    assert len(before) == 1
    conn.execute(update(t.users).where(t.users.c.id == demo_user).values(
        demo_date=template.row["scheduled_date"] + timedelta(days=2)))
    conn.commit()
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=headers(demo_user), json={"toPage": 19})
    assert response.status_code == 200, response.text
    assert passes(conn, unit_id)[0]["invalidated_at"] is not None
    conn.commit()
    record(api_client, demo_user, task_id)
    assert passes(conn, unit_id) == before


@pytest.mark.parametrize("kind", ["time", "excluded", "retired", "remedial", "review", "empty"])
def test_no_initial_from_unsupported_or_nonstudy_evidence(api_client, demo_user, domain_connection, kind):
    conn = domain_connection
    template, unit_id = fresh_unit(conn, demo_user)
    values = {"quantity_basis": "time"} if kind == "time" else {"in_scope": False} if kind == "excluded" else (
        {"retired_at": template.row["created_at"]} if kind == "retired" else {})
    if values:
        conn.execute(update(t.units).where(t.units.c.id == unit_id).values(**values))
    task_id = placement(conn, template, None if kind == "empty" else 12, None if kind == "empty" else 20,
                        task_type=kind if kind in ("remedial", "review") else "study")
    conn.commit()
    record(api_client, demo_user, task_id)
    assert passes(conn, unit_id) == []


def test_failure_after_pass_creation_rolls_back_record_and_pass(api_client, demo_user, domain_connection, monkeypatch):
    from app.services import records
    conn = domain_connection
    template, unit_id = fresh_unit(conn, demo_user)
    task_id = placement(conn, template, 12, 20)
    conn.commit()
    real = records.create_initial_passes

    def fail(*args):
        real(*args)
        raise RuntimeError("failure after pass insert")

    monkeypatch.setattr(records, "create_initial_passes", fail)
    response = api_client.post("/api/records", headers=headers(demo_user), json={"taskId": task_id, "result": "done"})
    assert response.status_code == 500
    assert passes(conn, unit_id) == []
    assert conn.scalar(select(t.study_records.c.id).where(t.study_records.c.task_id == task_id)) is None


def test_current_concept_union_can_complete_without_excluded_gap(api_client, demo_user, domain_connection):
    conn = domain_connection
    template, unit_id = fresh_unit(conn, demo_user)
    concepts = conn.execute(select(t.concepts).where(t.concepts.c.unit_id == unit_id)).mappings().all()
    assert concepts
    first_concept = concepts[0]
    conn.execute(update(t.concepts).where(t.concepts.c.id == first_concept["id"]).values(
        retired_at=None, in_scope=True, start_page=12, end_page=13))
    task_id = placement(conn, template, 12, 13)
    conn.commit()
    record(api_client, demo_user, task_id)
    item = passes(conn, unit_id)[0]
    assert item["evidence_snapshot"]["requiredRanges"] == [[12, 13]]
    assert len(item["evidence_snapshot"]["masteryScopeRevision"]) == 64


def test_another_users_record_cannot_complete_unit(api_client, demo_user, domain_connection):
    conn = domain_connection
    template, unit_id = fresh_unit(conn, demo_user)
    conn.commit()
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    foreign = task_for(conn, other)
    # Corrupt cross-owner historical ranges must not become learning evidence.
    conn.execute(t.study_record_ranges.insert().values(
        id=str(uuid4()), record_id=foreign.record["id"], unit_id=unit_id, start_page=14, end_page=20))
    task_id = placement(conn, template, 12, 13)
    conn.commit()
    record(api_client, demo_user, task_id)
    assert passes(conn, unit_id) == []


def test_concurrent_completion_and_retry_allocate_one_pass(api_client, demo_user, domain_connection, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.services import records
    from tests.test_focus_concurrency import fast_busy
    conn = domain_connection
    template, unit_id = fresh_unit(conn, demo_user)
    task_id = placement(conn, template, 12, 20)
    conn.commit()
    fast_busy(conn)
    entered, release = Event(), Event()
    real = records.create_initial_passes

    def held(*args):
        real(*args)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(records, "create_initial_passes", held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(record, api_client, demo_user, task_id)
        try:
            assert entered.wait(5)
            second = api_client.post("/api/records", headers=headers(demo_user),
                                     json={"taskId": task_id, "result": "done"})
            assert second.status_code == 409
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    record(api_client, demo_user, task_id)
    assert len(passes(conn, unit_id)) == 1


@pytest.mark.parametrize("invalidated", [False, True])
def test_full_relearn_consumed_evidence_cannot_be_reused_for_initial(
        api_client, demo_user, domain_connection, invalidated):
    conn = domain_connection
    task = task_for(conn, demo_user)
    unit_id = task.ranges[0]["unit_id"]
    old = passes(conn, unit_id)[0]
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == old["id"]).values(
        completion_kind="full_relearn", evidence_key=str(uuid4()),
        invalidated_at=old["completed_at"] if invalidated else None))
    conn.commit()
    record(api_client, demo_user, task.id)
    found = passes(conn, unit_id)
    assert len(found) == 1
    assert found[0]["completion_kind"] == "full_relearn"
