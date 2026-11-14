"""Strict range snapshots through the real request transaction on SQLite and PG."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from app.db import schema as t
from app.schemas.records import FixIn, RecordIn
from app.routers import today
from app.services import records
from app.services.study import StudyData
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


@pytest.mark.parametrize("body", [
    {}, {"toPage": None}, {"toPage": 0}, {"toPage": True}, {"toPage": "13"}, {"toPage": 13.0},
    {"completedRanges": None}, {"completedRanges": {}}, {"completedRanges": [None]},
    {"completedRanges": [{"rangeId": RANGE_ID, "toPage": False}]},
    {"toPage": 13, "completedRanges": []}, {"completedRanges": [], "sessionId": TASK_ID},
    {"completedRanges": [], "result": "done"},
])
def test_patch_schema_requires_exactly_one_strict_range_snapshot(body):
    with pytest.raises(ValidationError):
        FixIn.model_validate(body)


def test_patch_schema_accepts_an_explicit_empty_replacement():
    assert FixIn.model_validate({"completedRanges": []}).completedRanges == []


def patch(client, user, task, **fields):
    return client.patch(f"/api/tasks/{task.id}/record", headers=headers(user), json=fields)


def test_patch_transitions_partial_none_and_done_using_material_union(api_client, demo_user, domain_connection, mixed_task):
    task, ranges = mixed_task
    assert post(api_client, demo_user, task, "done").status_code == 200
    initial = snapshot(domain_connection, task)
    domain_connection.commit()
    snapshots = [
        ([{"rangeId": ranges[1]["id"], "toPage": 20}], "partial", 6, [None, 20, None]),
        ([], "none", 0, [None, None, None]),
        ([{"rangeId": row["id"], "toPage": row["end_page"]} for row in reversed(ranges)], "done", 14, [16, 20, 16]),
    ]
    for selected, status, pages, done_to in snapshots:
        response = patch(api_client, demo_user, task, completedRanges=selected)
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["taskId"] == task.id and result["status"] == status
        assert result["donePages"] == pages and result["doneTo"] is None
        assert [item["id"] for item in result["ranges"]] == [item["id"] for item in ranges]
        assert [item["doneTo"] for item in result["ranges"]] == done_to
        assert result["ranges"][0]["materialId"] == result["ranges"][1]["materialId"]
        assert result["ranges"][0]["materialId"] != result["ranges"][2]["materialId"]
        assert all(set(item) == {"id", "materialId", "unitId", "conceptId", "from", "to", "doneTo"}
                   for item in result["ranges"])
        saved = snapshot(domain_connection, task)
        assert saved["record"]["id"] == initial["record"]["id"]
        assert saved["task"]["status"] == saved["record"]["result"] == records.RESULT_TO_DB[status]
        domain_connection.commit()


def test_patch_single_range_supports_zero_partial_full_and_normalized_path(api_client, demo_user, domain_connection):
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.commit()
    assert post(api_client, demo_user, task, "done").status_code == 200
    for body, status, count, end in [({"completedRanges": []}, "none", 0, None),
                                   ({"toPage": 12}, "partial", 1, 12),
                                   ({"toPage": 20}, "done", 9, 20)]:
        response = api_client.patch(f"/api/tasks/{task.id.upper().replace('-', '')}/record",
                                    headers=headers(demo_user), json=body)
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert (result["taskId"], result["status"], result["donePages"], result["doneTo"]) == (task.id, status, count, end)
        assert result["ranges"][0]["doneTo"] == end


@pytest.mark.parametrize("fields", [
    {}, {"toPage": 0}, {"toPage": 21}, {"toPage": "13"}, {"toPage": 13, "completedRanges": []},
    {"sessionId": None, "completedRanges": []}, {"completedRanges": None}, {"completedRanges": [None]},
])
def test_patch_rejects_invalid_bodies_without_changing_records(api_client, demo_user, domain_connection, fields):
    task = task_for(domain_connection, demo_user, "DB2")
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    assert_error(patch(api_client, demo_user, task, **fields))
    assert snapshot(domain_connection, task) == before


@pytest.mark.parametrize("kind", ["duplicate", "foreign_range", "missing_range", "below", "above", "legacy_multi"])
def test_patch_validates_actual_owned_ranges(api_client, demo_user, domain_connection, mixed_task, kind):
    task, ranges = mixed_task
    assert post(api_client, demo_user, task, "done").status_code == 200
    selected = {"rangeId": ranges[0]["id"], "toPage": 13}
    fields = {"completedRanges": [selected]}
    if kind == "duplicate":
        fields["completedRanges"].append({**selected, "rangeId": selected["rangeId"].upper()})
    elif kind == "foreign_range":
        other = api_client.post("/api/session").json()["data"]["anonymousId"]
        selected["rangeId"] = task_for(domain_connection, other, "AI2").ranges[0]["id"]
    elif kind == "missing_range":
        selected["rangeId"] = str(uuid4())
    elif kind in ("below", "above"):
        selected["toPage"] = 11 if kind == "below" else 17
    elif kind == "legacy_multi":
        fields = {"toPage": 13}
    before = snapshot(domain_connection, task)
    domain_connection.commit()
    assert_error(patch(api_client, demo_user, task, **fields))
    assert snapshot(domain_connection, task) == before


@pytest.mark.parametrize("kind", ["unrecorded", "foreign_task", "missing_task", "invalid_uuid"])
def test_patch_requires_an_owned_existing_record(api_client, demo_user, domain_connection, kind):
    user = demo_user
    if kind == "foreign_task":
        user = api_client.post("/api/session").json()["data"]["anonymousId"]
    task = task_for(domain_connection, user, "DB2" if kind == "foreign_task" else "AI2")
    task_id = "bad-id" if kind == "invalid_uuid" else str(uuid4()) if kind == "missing_task" else task.id
    domain_connection.commit()
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=headers(demo_user), json={"completedRanges": []})
    status, code = (400, "NOT_RECORDED") if kind == "unrecorded" else (422, "INVALID_INPUT") if kind == "invalid_uuid" else (404, "TASK_NOT_FOUND")
    assert_error(response, status, code)


def test_patch_preserves_historical_record_date_time_and_linked_session(api_client, demo_user, domain_connection, end_focus):
    session = api_client.get("/api/study/sessions/active", headers=headers(demo_user)).json()["data"]
    end_focus(demo_user)
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.commit()
    response = post(api_client, demo_user, task, "partial", toPage=13, sessionId=session["id"])
    assert response.status_code == 200, response.text
    before = snapshot(domain_connection, task)["record"]
    old_session = dict(domain_connection.execute(select(t.study_sessions).where(
        t.study_sessions.c.id == session["id"])).mappings().one())
    domain_connection.execute(update(t.users).where(t.users.c.id == demo_user)
                              .values(demo_date=before["study_date"] + timedelta(days=2)))
    domain_connection.commit()
    for fields in [{"completedRanges": []}, {"toPage": 20}]:
        response = patch(api_client, demo_user, task, **fields)
        assert response.status_code == 200, response.text
        after = snapshot(domain_connection, task)["record"]
        for key in ("id", "task_id", "session_id", "recorded_at", "study_date"):
            assert after[key] == before[key]
        assert after["session_id"] == session["id"]
        assert dict(domain_connection.execute(select(t.study_sessions).where(
            t.study_sessions.c.id == session["id"])).mappings().one()) == old_session
        domain_connection.commit()


def test_range_snapshot_revalidates_only_existing_learning_pass(api_client, demo_user, domain_connection):
    task = task_for(domain_connection, demo_user, "DB2")
    before = dict(domain_connection.execute(select(t.learning_passes).where(
        t.learning_passes.c.unit_id == task.ranges[0]["unit_id"])).mappings().one())
    domain_connection.commit()
    for page, valid in [(13, False), (20, True)]:
        response = patch(api_client, demo_user, task, completedRanges=[{"rangeId": task.ranges[0]["id"], "toPage": page}])
        assert response.status_code == 200, response.text
        after = dict(domain_connection.execute(select(t.learning_passes).where(
            t.learning_passes.c.unit_id == task.ranges[0]["unit_id"])).mappings().one())
        assert (after["invalidated_at"] is None) is valid
        assert {**after, "invalidated_at": None} == before
        domain_connection.commit()


@pytest.mark.parametrize("method", ["post", "patch"])
def test_range_snapshot_rolls_back_all_writes_if_revalidation_fails(api_client, demo_user, domain_connection, monkeypatch, method):
    task = task_for(domain_connection, demo_user, "DB2")
    before = snapshot(domain_connection, task)
    old_passes = [dict(row) for row in domain_connection.execute(select(t.learning_passes)).mappings()]
    domain_connection.commit()
    real = records.revalidate_record_passes

    def fail(*args):
        real(*args)
        raise RuntimeError("failure after evidence mutation")

    monkeypatch.setattr(records, "revalidate_record_passes", fail)
    fields = {"completedRanges": [{"rangeId": task.ranges[0]["id"], "toPage": 13}]}
    response = post(api_client, demo_user, task, "partial", **fields) if method == "post" else patch(api_client, demo_user, task, **fields)
    assert_error(response, 500, "SERVER_ERROR")
    assert snapshot(domain_connection, task) == before
    assert [dict(row) for row in domain_connection.execute(select(t.learning_passes)).mappings()] == old_passes


@pytest.mark.parametrize("placement_status", ["carried_over", "excluded"])
@pytest.mark.parametrize("result", ["none", "partial", "done"])
@pytest.mark.parametrize("method", ["post", "patch"])
def test_record_correction_preserves_retired_placements_and_existing_evidence(
        api_client, demo_user, domain_connection, end_focus, placement_status, result, method):
    conn = domain_connection
    session = api_client.get("/api/study/sessions/active", headers=headers(demo_user)).json()["data"]
    end_focus(demo_user)
    task = task_for(conn, demo_user, "AI2")
    old_day = task.row["scheduled_date"]
    # Three remaining pages at five minutes/page meet the 15-minute minimum;
    # the former one-page fixture must now wait for replanning instead of carry.
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task.id).values(estimated_minutes=45))
    conn.commit()
    response = post(api_client, demo_user, task, "partial", toPage=17, sessionId=session["id"])
    assert response.status_code == 200, response.text
    original_record = snapshot(conn, task)["record"]

    # Existing evidence has its own historical scope, independent of placement.
    template = dict(conn.execute(select(t.learning_passes).limit(1)).mappings().one())
    historical_pass = {**template, "id": str(uuid4()), "unit_id": task.ranges[0]["unit_id"],
        "source_task_id": task.id, "completed_at": original_record["recorded_at"],
        "completed_date": original_record["study_date"], "invalidated_at": None,
        "evidence_snapshot": {"schemaVersion": "1", "recordIds": [original_record["id"]],
                              "requiredRanges": [[12, 17]]}}
    conn.execute(t.learning_passes.insert().values(**historical_pass))
    if placement_status == "excluded":
        # The actual next-day API excludes placements on/after the exam date.
        conn.execute(update(t.subjects).where(t.subjects.c.id == task.row["subject_id"])
                     .values(exam_date=old_day))
    conn.commit()
    response = api_client.post("/api/demo/next-day", headers=headers(demo_user))
    assert response.status_code == 200, response.text
    assert snapshot(conn, task)["task"]["status"] == placement_status

    plan_tasks = select(t.study_tasks).where(t.study_tasks.c.plan_id == task.row["plan_id"]).order_by(t.study_tasks.c.id)
    before_tasks = [dict(row) for row in conn.execute(plan_tasks).mappings()]
    task_ids = [row["id"] for row in before_tasks]
    plan_ranges = select(t.study_task_ranges).where(t.study_task_ranges.c.task_id.in_(task_ids)).order_by(t.study_task_ranges.c.id)
    before_ranges = [dict(row) for row in conn.execute(plan_ranges).mappings()]
    children = [row for row in before_tasks if row["origin_task_id"] == task.id]
    assert len(children) == (1 if placement_status == "carried_over" else 0)
    if children:
        assert children[0]["status"] == "planned" and children[0]["block_id"] == task.row["block_id"]
    before_passes = [dict(row) for row in conn.execute(select(t.learning_passes).order_by(t.learning_passes.c.id)).mappings()]
    before_sessions = [dict(row) for row in conn.execute(select(t.study_sessions).order_by(t.study_sessions.c.id)).mappings()]
    before_block_ids = set(conn.scalars(select(t.study_blocks.c.id)))
    conn.commit()

    selected = [] if result == "none" else [{"rangeId": task.ranges[0]["id"], "toPage": 13 if result == "partial" else 20}]
    response = post(api_client, demo_user, task, result, completedRanges=selected) if method == "post" else patch(
        api_client, demo_user, task, completedRanges=selected)
    assert response.status_code == 200, response.text
    if method == "patch":
        assert response.json()["data"]["status"] == result

    after_tasks = [dict(row) for row in conn.execute(plan_tasks).mappings()]
    after_by_id = {row["id"]: row for row in after_tasks}
    new_tasks = [row for row in after_tasks if row["id"] not in task_ids]
    child_id = children[0]["id"] if children else None
    for before in before_tasks:
        after = after_by_id[before["id"]]
        if before["id"] == child_id:
            assert after == {**before, "status": "excluded" if result == "done" else "carried_over"}
        else:
            # Original retired placements and every unrelated placement remain
            # byte-for-byte unchanged even when an untouched carry is replaced.
            assert after == before
    if children and result != "done":
        assert len(new_tasks) == 1
        replacement = new_tasks[0]
        assert replacement["origin_task_id"] == child_id
        assert replacement["block_id"] == task.row["block_id"]
        assert replacement["status"] == "planned"
        assert replacement["scheduled_date"] == children[0]["scheduled_date"]
        assert replacement["estimated_minutes"] == (45 if result == "none" else 35)
        replacement_ranges = list(conn.execute(select(t.study_task_ranges).where(
            t.study_task_ranges.c.task_id == replacement["id"])).mappings())
        # The original carry contributes 18..20; restoring earlier pages may
        # produce a second range, but must cover exactly the required PDF pages.
        assert {page for row in replacement_ranges for page in range(row["start_page"], row["end_page"] + 1)} == set(
            range(12 if result == "none" else 14, 21))
        assert sum(row["end_page"] - row["start_page"] + 1 for row in replacement_ranges) == (9 if result == "none" else 7)
        assert all(row["unit_id"] == task.ranges[0]["unit_id"] for row in replacement_ranges)
    else:
        assert not new_tasks
    assert [dict(row) for row in conn.execute(plan_ranges).mappings()] == before_ranges
    assert [dict(row) for row in conn.execute(select(t.study_sessions).order_by(t.study_sessions.c.id)).mappings()] == before_sessions
    assert task.id not in {row.id for row in StudyData(conn, demo_user).tasks_on(old_day)}
    assert set(conn.scalars(select(t.study_blocks.c.id))) == before_block_ids
    saved = snapshot(conn, task)
    assert saved["record"]["result"] == records.RESULT_TO_DB[result]
    for key in ("id", "task_id", "study_date", "recorded_at", "session_id"):
        assert saved["record"][key] == original_record[key]
    assert [(row["start_page"], row["end_page"]) for row in saved["ranges"]] == (
        [] if result == "none" else [(12, 13 if result == "partial" else 20)])
    after_passes = [dict(row) for row in conn.execute(select(t.learning_passes).order_by(t.learning_passes.c.id)).mappings()]
    for before, after in zip(before_passes, after_passes, strict=True):
        if before["id"] == historical_pass["id"]:
            assert (after["invalidated_at"] is None) is (result == "done")
            assert {**after, "invalidated_at": None} == before
        else:
            assert after == before
