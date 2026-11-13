from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.routers import today
from tests.test_record_transactions import headers, task_for


def learning_pass(conn, unit_id):
    return dict(conn.execute(select(t.learning_passes).where(
        t.learning_passes.c.unit_id == unit_id)).mappings().one())


def patch(api_client, user, task, page):
    response = api_client.patch(f"/api/tasks/{task.id}/record", headers=headers(user), json={"toPage": page})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def add_evidence(conn, task, unit_id, ranges):
    """별도 태스크의 실제 기록. 같은 단원의 여러 날짜 근거를 재현한다."""
    task_id, record_id = str(uuid4()), str(uuid4())
    values = {**task.row, "id": task_id, "scheduled_date": task.row["scheduled_date"] - timedelta(days=1)}
    conn.execute(t.study_tasks.insert().values(**values))
    conn.execute(t.study_records.insert().values(
        id=record_id, task_id=task_id, result="partial", study_date=values["scheduled_date"],
        recorded_at=values["created_at"], updated_at=values["created_at"]))
    for start, end in ranges:
        conn.execute(t.study_record_ranges.insert().values(
            id=str(uuid4()), record_id=record_id, unit_id=unit_id, start_page=start, end_page=end))
    return record_id


def link_evidence(conn, item, extra_ids, required=None):
    snapshot = deepcopy(item["evidence_snapshot"])
    snapshot["recordIds"] += extra_ids
    if required is not None:
        snapshot["requiredRanges"] = required
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == item["id"])
                 .values(evidence_snapshot=snapshot))


def protected_rows(conn):
    return {table.name: [dict(row) for row in conn.execute(select(table)).mappings()]
            for table in (t.daily_plan_totals, t.quiz_sessions, t.quiz_attempts, t.mastery_history, t.study_blocks)}


def test_complete_partial_complete_preserves_history_and_reactivates_same_pass(
        api_client, demo_user, domain_connection):
    conn = domain_connection
    task = task_for(conn, demo_user)
    before = learning_pass(conn, task.ranges[0]["unit_id"])
    other_passes = [dict(row) for row in conn.execute(select(t.learning_passes).where(
        t.learning_passes.c.id != before["id"])).mappings()]
    protected = protected_rows(conn)
    conn.execute(update(t.users).where(t.users.c.id == demo_user)
                 .values(demo_date=task.record["study_date"] + timedelta(days=1)))
    conn.commit()
    assert patch(api_client, demo_user, task, 13)["status"] == "partial"
    partial = learning_pass(conn, task.ranges[0]["unit_id"])
    assert partial["invalidated_at"] is not None
    assert {**partial, "invalidated_at": None} == before
    conn.commit()
    patch(api_client, demo_user, task, 13)
    assert learning_pass(conn, task.ranges[0]["unit_id"]) == partial
    conn.commit()
    assert patch(api_client, demo_user, task, 20)["status"] == "done"
    assert learning_pass(conn, task.ranges[0]["unit_id"]) == before
    after = task_for(conn, demo_user).record
    assert after["id"] == task.record["id"]
    assert after["study_date"] == task.record["study_date"]
    assert after["recorded_at"] == task.record["recorded_at"]
    assert protected_rows(conn) == protected
    assert [dict(row) for row in conn.execute(select(t.learning_passes).where(
        t.learning_passes.c.id != before["id"])).mappings()] == other_passes


def test_post_none_and_done_revalidate_existing_pass(api_client, demo_user, domain_connection):
    conn = domain_connection
    task = task_for(conn, demo_user)
    before = learning_pass(conn, task.ranges[0]["unit_id"])
    conn.commit()
    for result in ("none", "none", "done", "done"):
        response = api_client.post("/api/records", headers=headers(demo_user),
                                   json={"taskId": task.id, "result": result})
        assert response.status_code == 200, response.text
        item = learning_pass(conn, task.ranges[0]["unit_id"])
        assert (item["invalidated_at"] is None) == (result == "done")
        assert {**item, "invalidated_at": None} == before
        conn.commit()


@pytest.mark.parametrize("ranges,required,valid", [
    ([(14, 20)], [[12, 20]], True),
    ([(13, 16), (16, 20)], [[12, 20]], True),
    ([(15, 20)], [[12, 20]], False),  # 한 쪽 구멍도 실패
    ([(20, 20)], [[12, 20]], False),  # 마지막 페이지 도달만으로 완료 아님
    ([(19, 20)], [[12, 13], [19, 20]], True),  # 비연속 고정 요구 범위
    ([], [[12, 20]], False),
])
def test_union_of_only_fixed_evidence_records(api_client, demo_user, domain_connection, ranges, required, valid):
    conn = domain_connection
    task = task_for(conn, demo_user)
    unit_id = task.ranges[0]["unit_id"]
    item = learning_pass(conn, unit_id)
    extra_id = add_evidence(conn, task, unit_id, ranges)
    link_evidence(conn, item, [extra_id], required)
    before = learning_pass(conn, unit_id)
    conn.commit()
    patch(api_client, demo_user, task, 13)
    after = learning_pass(conn, unit_id)
    assert (after["invalidated_at"] is None) == valid
    assert {**after, "invalidated_at": None} == before


@pytest.mark.parametrize("kind", ["unlinked", "other_unit", "foreign_user", "missing"])
def test_unrelated_or_unowned_records_do_not_repair_pass(api_client, demo_user, domain_connection, kind):
    conn = domain_connection
    task = task_for(conn, demo_user)
    unit_id = task.ranges[0]["unit_id"]
    item = learning_pass(conn, unit_id)
    if kind == "foreign_user":
        conn.commit()
        other = api_client.post("/api/session").json()["data"]["anonymousId"]
        other_task = task_for(conn, other)
        other_pass = learning_pass(conn, other_task.ranges[0]["unit_id"])
        extra_id = other_task.record["id"]
    elif kind == "missing":
        extra_id = str(uuid4())
    else:
        other_unit = task_for(conn, demo_user, "OS2").ranges[0]["unit_id"] if kind == "other_unit" else unit_id
        extra_id = add_evidence(conn, task, other_unit, [(12, 20)])
    if kind != "unlinked":
        link_evidence(conn, item, [extra_id])
    conn.commit()
    patch(api_client, demo_user, task, 13)
    assert learning_pass(conn, unit_id)["invalidated_at"] is not None
    if kind == "foreign_user":
        assert learning_pass(conn, other_task.ranges[0]["unit_id"]) == other_pass


def test_current_unit_scope_does_not_replace_historical_required_ranges(api_client, demo_user, domain_connection):
    conn = domain_connection
    task = task_for(conn, demo_user)
    unit_id = task.ranges[0]["unit_id"]
    conn.execute(update(t.units).where(t.units.c.id == unit_id).values(end_page=13))
    conn.commit()
    patch(api_client, demo_user, task, 13)
    assert learning_pass(conn, unit_id)["invalidated_at"] is not None
    conn.commit()
    patch(api_client, demo_user, task, 20)
    assert learning_pass(conn, unit_id)["invalidated_at"] is None


@pytest.mark.parametrize("failure", ["after_revalidation", "unsupported_snapshot"])
def test_evidence_failure_rolls_back_record_ranges_task_and_pass(
        api_client, demo_user, domain_connection, monkeypatch, failure):
    conn = domain_connection
    task = task_for(conn, demo_user)
    unit_id = task.ranges[0]["unit_id"]
    if failure == "after_revalidation":
        real = today.revalidate_record_passes

        def fail(*args):
            real(*args)
            raise RuntimeError("test failure after evidence writes")

        monkeypatch.setattr(today, "revalidate_record_passes", fail)
    else:
        item = learning_pass(conn, unit_id)
        link_evidence(conn, item, [], [{"nominalMinutes": 25}])
    before = learning_pass(conn, unit_id)
    conn.commit()
    response = api_client.patch(f"/api/tasks/{task.id}/record", headers=headers(demo_user), json={"toPage": 13})
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "SERVER_ERROR"
    after = task_for(conn, demo_user)
    assert after.row == task.row
    assert after.record == task.record
    assert after.done_ranges == task.done_ranges
    assert learning_pass(conn, unit_id) == before
