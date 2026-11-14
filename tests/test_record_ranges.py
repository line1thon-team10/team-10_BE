"""Strict range snapshots through the real request transaction on SQLite and PG."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from app.db import schema as t
from app.schemas.records import RecordIn
from app.routers import today
from tests.test_record_transactions import headers, task_for

TASK_ID = "2d4b385a-ec4a-4e95-b313-a98ce1641d42"
RANGE_ID = "536b5488-c9f9-46ca-b382-bbf66021e26d"


def configure_ranges(conn, user, definitions):
    task = task_for(conn, user, "AI2")
    unit = dict(conn.execute(select(t.units).where(t.units.c.id == task.ranges[0]["unit_id"])).mappings().one())
    material = dict(conn.execute(select(t.materials).where(t.materials.c.id == unit["material_id"])).mappings().one())
    unit_ids = {"a": unit["id"]}
    if any(pdf != "a" for pdf, _, _ in definitions):
        new_material, new_unit = str(uuid4()), str(uuid4())
        conn.execute(t.materials.insert().values(**{**material, "id": new_material, "sha256": uuid4().hex}))
        conn.execute(t.units.insert().values(**{**unit, "id": new_unit, "material_id": new_material}))
        unit_ids["b"] = new_unit
    conn.execute(t.study_task_ranges.delete().where(t.study_task_ranges.c.task_id == task.id))
    ranges = []
    for order, (pdf, start, end) in enumerate(definitions):
        item = {"id": str(uuid4()), "task_id": task.id, "unit_id": unit_ids[pdf], "concept_id": None,
                "start_page": start, "end_page": end, "sort_order": order}
        conn.execute(t.study_task_ranges.insert().values(**item))
        ranges.append(item)
    conn.commit()
    return task, ranges


@pytest.fixture
def mixed_task(domain_connection, demo_user):
    return configure_ranges(domain_connection, demo_user, [("a", 12, 16), ("a", 15, 20), ("b", 12, 16)])


def snapshot(conn, task):
    record = conn.execute(select(t.study_records).where(t.study_records.c.task_id == task.id)).mappings().first()
    return {
        "record": dict(record) if record else None,
        "ranges": [dict(row) for row in conn.execute(select(t.study_record_ranges).where(
            t.study_record_ranges.c.record_id == record["id"]).order_by(t.study_record_ranges.c.id)).mappings()] if record else [],
        "task": dict(conn.execute(select(t.study_tasks).where(t.study_tasks.c.id == task.id)).mappings().one()),
        "block": dict(conn.execute(select(t.study_blocks).where(
            t.study_blocks.c.id == task.row["block_id"])).mappings().one()),
    }


def post(client, user, task, result, **fields):
    return client.post("/api/records", headers=headers(user), json={"taskId": task.id, "result": result, **fields})


def assert_error(response, status=422, code="INVALID_INPUT"):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("field", ["taskId", "sessionId"])
@pytest.mark.parametrize("invalid", [None, "", "task-a", 1, True, [], {}, UUID(TASK_ID)])
def test_record_schema_requires_uuid_strings(field, invalid):
    with pytest.raises(ValidationError):
        RecordIn.model_validate({"taskId": TASK_ID, "result": "done", field: invalid})


@pytest.mark.parametrize("page", [None, 0, -1, True, False, "13", 13.0, 13.5, float("nan"), float("inf")])
@pytest.mark.parametrize("nested", [False, True])
def test_record_schema_rejects_nonpositive_or_coercible_pages(page, nested):
    fields = {"completedRanges": [{"rangeId": RANGE_ID, "toPage": page}]} if nested else {"toPage": page}
    with pytest.raises(ValidationError):
        RecordIn.model_validate({"taskId": TASK_ID, "result": "partial", **fields})


@pytest.mark.parametrize("fields", [
    {"completedRanges": None}, {"completedRanges": {}}, {"completedRanges": ()},
    {"completedRanges": [None]}, {"completedRanges": [{"rangeId": RANGE_ID}]},
    {"completedRanges": [{"rangeId": "wrong", "toPage": 13}]},
    {"completedRanges": [{"rangeId": RANGE_ID, "toPage": 13, "materialId": TASK_ID}]},
    {"completedRanges": [{"rangeId": RANGE_ID, "toPage": 13}] * 2},
    {"completedRanges": [{"rangeId": RANGE_ID, "toPage": 13}, {"rangeId": RANGE_ID.upper().replace("-", ""), "toPage": 14}]},
    {"completedRanges": [], "toPage": 13}, {"donePages": 1}, {"result": "unknown"},
])
def test_record_schema_rejects_unknown_keys_shapes_and_duplicate_normalized_ids(fields):
    with pytest.raises(ValidationError):
        RecordIn.model_validate({"taskId": TASK_ID, "result": "partial", **fields})


def test_record_schema_normalizes_ids_without_coercing_pages():
    body = RecordIn.model_validate({"taskId": TASK_ID.upper().replace("-", ""), "result": "partial",
        "sessionId": TASK_ID.upper(), "completedRanges": [{"rangeId": RANGE_ID.upper(), "toPage": 13}]})

    assert body.taskId == body.sessionId == TASK_ID
    assert body.completedRanges[0].rangeId == RANGE_ID
    assert type(body.completedRanges[0].toPage) is int


def test_post_replaces_all_ranges_and_keeps_last_page_only_partial(api_client, demo_user, domain_connection, mixed_task):
    task, ranges = mixed_task
    response = post(api_client, demo_user, task, "done")
    assert response.status_code == 200, response.text
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    response = post(api_client, demo_user, task, "partial",
                    completedRanges=[{"rangeId": ranges[1]["id"], "toPage": 20}])
    assert response.status_code == 200, response.text
    after = snapshot(domain_connection, task)
    assert after["task"]["status"] == after["record"]["result"] == "partial"
    assert after["record"]["id"] == before["record"]["id"]
    assert after["record"]["recorded_at"] == before["record"]["recorded_at"]
    assert after["record"]["study_date"] == before["record"]["study_date"]
    assert [(row["unit_id"], row["start_page"], row["end_page"]) for row in after["ranges"]] == [(ranges[1]["unit_id"], 15, 20)]


def test_post_partial_keeps_equal_page_numbers_in_different_pdfs_separate(api_client, demo_user, domain_connection):
    task, ranges = configure_ranges(domain_connection, demo_user, [("a", 12, 16), ("b", 12, 16)])
    response = post(api_client, demo_user, task, "partial", completedRanges=[{"rangeId": ranges[1]["id"], "toPage": 16}])
    assert response.status_code == 200, response.text
    assert snapshot(domain_connection, task)["task"]["status"] == "partial"


@pytest.mark.parametrize("result,valid", [("partial", False), ("done", True)])
def test_post_result_uses_union_instead_of_sum_of_overlapping_ranges(api_client, demo_user, domain_connection, result, valid):
    task, ranges = configure_ranges(domain_connection, demo_user, [("a", 12, 20), ("a", 15, 20)])
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    response = post(api_client, demo_user, task, result, completedRanges=[{"rangeId": ranges[0]["id"], "toPage": 20}])
    if valid:
        assert response.status_code == 200, response.text
        assert snapshot(domain_connection, task)["task"]["status"] == "completed"
    else:
        assert_error(response)
        assert snapshot(domain_connection, task) == before


@pytest.mark.parametrize("kind", ["empty_partial", "full_partial", "missing_partial", "partial_done", "nonempty_none", "legacy_multi", "two_inputs", "below", "above"])
def test_post_rejects_invalid_range_meaning_without_writes(api_client, demo_user, domain_connection, mixed_task, kind):
    task, ranges = mixed_task
    result = "partial"
    fields = {"completedRanges": [{"rangeId": ranges[0]["id"], "toPage": 13}]}
    if kind == "empty_partial":
        fields = {"completedRanges": []}
    elif kind == "full_partial":
        fields = {"completedRanges": [{"rangeId": item["id"], "toPage": item["end_page"]} for item in ranges]}
    elif kind == "missing_partial":
        fields = {}
    elif kind == "partial_done":
        result = "done"
    elif kind == "nonempty_none":
        result = "none"
    elif kind == "legacy_multi":
        fields = {"toPage": 13}
    elif kind == "two_inputs":
        fields["toPage"] = 13
    elif kind in ("below", "above"):
        fields["completedRanges"][0]["toPage"] = 11 if kind == "below" else 17
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    assert_error(post(api_client, demo_user, task, result, **fields))
    assert snapshot(domain_connection, task) == before


@pytest.mark.parametrize("kind", ["missing", "other_task", "foreign_user"])
def test_post_rejects_ranges_outside_the_owned_task(api_client, demo_user, domain_connection, mixed_task, kind):
    task, ranges = mixed_task
    if kind == "missing":
        range_id = str(uuid4())
    elif kind == "other_task":
        range_id = task_for(domain_connection, demo_user, "DB2").ranges[0]["id"]
    else:
        other = api_client.post("/api/session").json()["data"]["anonymousId"]
        range_id = task_for(domain_connection, other, "AI2").ranges[0]["id"]
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    assert_error(post(api_client, demo_user, task, "partial", completedRanges=[{"rangeId": range_id, "toPage": 13}]))
    assert snapshot(domain_connection, task) == before


@pytest.mark.parametrize("fields", [{"sessionId": None}, {"toPage": "13"}, {"extra": True}, {"taskId": "bad"}])
def test_post_applies_strict_schema_before_database_queries(api_client, demo_user, domain_connection, fields):
    task = task_for(domain_connection, demo_user, "AI2")
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    assert_error(api_client.post("/api/records", headers=headers(demo_user),
                 json={"taskId": task.id, "result": "partial", "toPage": 13, **fields}))
    assert snapshot(domain_connection, task) == before


def test_post_accepts_normalized_uuids_for_task_and_range(api_client, demo_user, domain_connection):
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.commit()
    response = api_client.post("/api/records", headers=headers(demo_user), json={
        "taskId": task.id.upper().replace("-", ""), "result": "partial",
        "completedRanges": [{"rangeId": task.ranges[0]["id"].upper(), "toPage": 13}]})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("nominal", [False, True])
def test_post_unpaged_task_accepts_done_and_none_but_rejects_partial(api_client, demo_user, domain_connection, nominal):
    task = task_for(domain_connection, demo_user, "AI2")
    if nominal:
        domain_connection.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task.id)
                                  .values(start_page=None, end_page=None, planned_minutes=25))
    else:
        domain_connection.execute(t.study_task_ranges.delete().where(t.study_task_ranges.c.task_id == task.id))
    domain_connection.commit()
    assert_error(post(api_client, demo_user, task, "partial", completedRanges=[]), 400, "BAD_PAGE")
    for result, status in [("done", "completed"), ("none", "missed")]:
        response = post(api_client, demo_user, task, result)
        assert response.status_code == 200, response.text
        saved = snapshot(domain_connection, task)
        assert saved["task"]["status"] == saved["record"]["result"] == status
        assert saved["ranges"] == []
        domain_connection.commit()


def test_post_foreign_task_is_not_found(api_client, demo_user, domain_connection):
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    task = task_for(domain_connection, other, "AI2")
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    assert_error(post(api_client, demo_user, task, "done"), 404, "TASK_NOT_FOUND")
    assert snapshot(domain_connection, task) == before


@pytest.mark.parametrize("page", [12, 19, 20])
def test_post_single_range_to_page_preserves_partial_only_contract(api_client, demo_user, domain_connection, page):
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.commit()
    response = post(api_client, demo_user, task, "partial", toPage=page)
    if page == 20:
        assert_error(response)
    else:
        assert response.status_code == 200, response.text
        assert task_for(domain_connection, demo_user, "AI2").done_pages == page - 11


@pytest.mark.parametrize("kind", ["foreign_subject", "foreign_material"])
def test_post_rejects_inconsistent_parent_ownership(api_client, demo_user, domain_connection, kind):
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    task = task_for(domain_connection, demo_user, "AI2")
    other_task = task_for(domain_connection, other, "AI2")
    if kind == "foreign_subject":
        domain_connection.execute(update(t.study_tasks).where(t.study_tasks.c.id == task.id)
                                  .values(subject_id=other_task.row["subject_id"]))
    else:
        domain_connection.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task.id)
                                  .values(unit_id=other_task.ranges[0]["unit_id"]))
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    assert_error(post(api_client, demo_user, task, "done"), 404, "TASK_NOT_FOUND")
    assert snapshot(domain_connection, task) == before


@pytest.mark.parametrize("kind", ["running", "foreign_user", "different_task"])
def test_post_checks_session_owner_task_and_ended_state(api_client, demo_user, domain_connection, kind):
    user = demo_user
    if kind == "foreign_user":
        user = api_client.post("/api/session").json()["data"]["anonymousId"]
    session = api_client.get("/api/study/sessions/active", headers=headers(user)).json()["data"]
    task = task_for(domain_connection, demo_user, "DB2" if kind == "different_task" else "AI2")
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    response = post(api_client, demo_user, task, "done", sessionId=session["id"])
    assert_error(response, 409 if kind == "running" else 404,
                 "SESSION_RUNNING" if kind == "running" else "SESSION_NOT_FOUND")
    assert snapshot(domain_connection, task) == before


def test_post_uses_one_timestamp_for_record_and_timezone_date(api_client, demo_user, domain_connection, monkeypatch):
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.execute(update(t.users).where(t.users.c.id == demo_user).values(demo_date=None))
    domain_connection.commit()
    at = datetime(2026, 11, 13, 15, 5, tzinfo=timezone.utc)
    calls = []

    def clock():
        calls.append(True)
        assert len(calls) == 1
        return at

    monkeypatch.setattr(today, "now_utc", clock)
    response = post(api_client, demo_user, task, "partial", toPage=13)
    assert response.status_code == 200, response.text
    record = snapshot(domain_connection, task)["record"]
    assert record["study_date"].isoformat() == "2026-11-14"
    assert record["recorded_at"].replace(tzinfo=timezone.utc) == at
    assert calls == [True]
