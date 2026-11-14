"""Direct relearning records can be corrected without becoming plan placements."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_initial_pass import passes, placement
from tests.test_record_transactions import headers, task_for


def direct_record(conn, user):
    template = task_for(conn, user)
    uid = template.ranges[0]["unit_id"]
    task_id = placement(conn, template, 12, 20, task_type="review")
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task_id).values(plan_id=None, status="completed"))
    rec_id, pass_id = str(uuid4()), str(uuid4())
    at = template.record["recorded_at"]
    conn.execute(t.study_records.insert().values(id=rec_id, task_id=task_id, result="completed",
        study_date=template.record["study_date"], recorded_at=at, updated_at=at))
    conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=rec_id, unit_id=uid,
                                                       start_page=12, end_page=20))
    conn.execute(t.learning_passes.insert().values(id=pass_id, unit_id=uid, pass_number=2,
        completion_kind="full_relearn", source_task_id=task_id, evidence_key=str(uuid4()),
        evidence_snapshot={"schemaVersion": "1", "recordIds": [rec_id], "requiredRanges": [[12, 20]]},
        completed_at=at, completed_date=template.record["study_date"]))
    return template, task_id, rec_id, pass_id


def fixed_rows(conn):
    return {table.name: [dict(row) for row in conn.execute(select(table)).mappings()]
            for table in (t.daily_plan_totals, t.plans, t.remedial_obligations, t.quiz_sessions, t.mastery_history)}


def test_direct_record_patch_preserves_date_and_only_revalidates_its_pass(api_client, demo_user, domain_connection):
    conn = domain_connection
    template, task_id, rec_id, pass_id = direct_record(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    original = passes(conn, uid)
    protected = fixed_rows(conn)
    original_record = dict(conn.execute(select(t.study_records).where(t.study_records.c.id == rec_id)).mappings().one())
    conn.execute(update(t.users).where(t.users.c.id == demo_user).values(
        demo_date=template.record["study_date"] + timedelta(days=2)))
    conn.commit()
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=headers(demo_user), json={"toPage": 13})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["donePages"] == 2
    partial = passes(conn, uid)
    assert partial[0] == original[0]
    assert partial[1]["invalidated_at"] is not None
    assert fixed_rows(conn) == protected
    conn.commit()
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=headers(demo_user), json={"toPage": 20})
    assert response.status_code == 200, response.text
    assert passes(conn, uid) == original
    after = dict(conn.execute(select(t.study_records).where(t.study_records.c.id == rec_id)).mappings().one())
    assert after["recorded_at"] == original_record["recorded_at"]
    assert after["study_date"] == original_record["study_date"]
    assert after["session_id"] is None
    assert conn.scalar(select(t.study_tasks.c.plan_id).where(t.study_tasks.c.id == task_id)) is None
    assert fixed_rows(conn) == protected


@pytest.mark.parametrize("invalid", ["post", "owner", "study", "foreign_range"])
def test_planless_fallback_is_patch_only_owned_review(api_client, demo_user, domain_connection, invalid):
    conn = domain_connection
    template, task_id, rec_id, pass_id = direct_record(conn, demo_user)
    conn.commit()
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    if invalid == "study":
        conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task_id).values(task_type="study"))
    if invalid == "foreign_range":
        foreign_uid = task_for(conn, other).ranges[0]["unit_id"]
        conn.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task_id).values(unit_id=foreign_uid))
    before = dict(conn.execute(select(t.study_records).where(t.study_records.c.id == rec_id)).mappings().one())
    conn.commit()
    if invalid == "post":
        response = api_client.post("/api/records", headers=headers(demo_user), json={"taskId": task_id, "result": "done"})
    else:
        response = api_client.patch(f"/api/tasks/{task_id}/record", headers=headers(other if invalid == "owner" else demo_user),
                                    json={"toPage": 13})
    assert response.status_code == 404, response.text
    assert dict(conn.execute(select(t.study_records).where(t.study_records.c.id == rec_id)).mappings().one()) == before
