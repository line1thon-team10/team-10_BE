"""Shared read projection and its real home/focus HTTP consumers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.services.study import StudyData
from app.services.task_views import TaskView
from tests.test_focus_state import active, action, clock
from tests.test_record_ranges import configure_ranges, post
from tests.test_record_transactions import headers, task_for


def planned(identifier="range-a", material="pdf-a", unit="unit-a", start=1, end=5, concept=None):
    return {"id": identifier, "material_id": material, "unit_id": unit, "concept_id": concept,
            "start_page": start, "end_page": end}


def completed(unit="unit-a", start=1, end=5):
    return {"unit_id": unit, "start_page": start, "end_page": end}


def test_task_view_preserves_order_identity_and_unions_pages_per_pdf():
    ranges = [planned("r-b", "pdf-b", "unit-b", concept="concept-b"),
              planned("r-a", end=7), planned("r-overlap", start=3, end=5)]
    done = [completed("unit-b", 1, 2), completed(start=1, end=4), completed(start=3, end=4)]
    before = deepcopy((ranges, done))

    view = TaskView.from_ranges(iter(ranges), iter(done), unit_code="AI2").as_dict()

    assert (view["pages"], view["donePages"]) == (12, 6)
    assert view["from"] is view["to"] is view["doneTo"] is view["unitNo"] is None
    assert view["ranges"] == [
        {"id": "r-b", "materialId": "pdf-b", "unitId": "unit-b", "conceptId": "concept-b",
         "from": 1, "to": 5, "doneTo": 2},
        {"id": "r-a", "materialId": "pdf-a", "unitId": "unit-a", "conceptId": None,
         "from": 1, "to": 7, "doneTo": 4},
        {"id": "r-overlap", "materialId": "pdf-a", "unitId": "unit-a", "conceptId": None,
         "from": 3, "to": 5, "doneTo": 4},
    ]
    assert (ranges, done) == before


@pytest.mark.parametrize("done,done_pages,done_to", [
    ([], 0, None),
    ([completed(start=1, end=20)], 8, 10),
    ([completed(start=1, end=4), completed(start=8, end=20)], 5, 4),
    ([completed(start=8, end=20)], 3, None),
    ([completed(start=3, end=4), completed(start=5, end=7)], 5, 7),
    ([completed(start=1, end=2), completed(start=11, end=20)], 0, None),
    ([completed("other-unit", 3, 10)], 0, None),
    ([completed(start=None, end=None)], 0, None),
])
def test_task_view_clips_current_unit_evidence_and_keeps_contiguous_prefix(done, done_pages, done_to):
    view = TaskView.from_ranges([planned(start=3, end=10)], done, unit_code="AI2").as_dict()
    assert (view["from"], view["to"], view["pages"], view["unitNo"]) == (3, 10, 8, 2)
    assert view["donePages"] == done_pages
    assert view["doneTo"] == view["ranges"][0]["doneTo"] == done_to


def test_task_view_overlapping_units_share_page_totals_but_not_completion_evidence():
    view = TaskView.from_ranges([planned(), planned("r-b", unit="unit-b")], [completed()]).as_dict()
    assert (view["pages"], view["donePages"]) == (5, 5)
    assert [item["doneTo"] for item in view["ranges"]] == [5, None]


@pytest.mark.parametrize("ranges", [[], [planned(start=None, end=None)]])
def test_task_view_empty_or_nominal_ranges_do_not_imply_completion(ranges):
    view = TaskView.from_ranges(ranges, [completed()], unit_code="AI2").as_dict()
    assert view == {"ranges": [], "pages": 0, "donePages": 0,
                    "from": None, "to": None, "doneTo": None, "unitNo": None}


def test_task_view_paged_and_nominal_mix_has_no_single_range_shortcut():
    view = TaskView.from_ranges([planned(), planned("nominal", start=None, end=None)],
                                [completed()], unit_code="AI2").as_dict()
    assert (view["pages"], view["donePages"], len(view["ranges"])) == (5, 5, 1)
    assert view["from"] is view["to"] is view["doneTo"] is view["unitNo"] is None


def test_task_view_counts_large_ranges_without_materializing_each_page():
    view = TaskView.from_ranges([planned(end=10**12)], [completed(end=10**12 - 1)])
    assert view.pages == 10**12
    assert view.done_pages == view.done_to == 10**12 - 1


@pytest.mark.parametrize("start,end", [(0, 5), (5, 4), (True, 5), (1.0, 5), (1, "5")])
def test_task_view_does_not_coerce_invalid_page_bounds(start, end):
    with pytest.raises(ValueError):
        TaskView.from_ranges([planned(start=start, end=end)])


def home(client, user):
    response = client.get("/api/today", headers=headers(user))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def home_task(client, user, task_id):
    return next(task for task in home(client, user)["tasks"] if task["id"] == task_id)


def test_today_returns_ordered_range_identity_and_normalized_progress_inputs(api_client, demo_user, domain_connection):
    task, ranges = configure_ranges(domain_connection, demo_user, [("a", 15, 20), ("a", 12, 16), ("b", 12, 16)])
    result = post(api_client, demo_user, task, "partial", completedRanges=[
        {"rangeId": ranges[0]["id"], "toPage": 17}, {"rangeId": ranges[2]["id"], "toPage": 13}])
    assert result.status_code == 200, result.text

    data = home(api_client, demo_user)
    view = next(item for item in data["tasks"] if item["id"] == task.id)
    materials = dict(domain_connection.execute(select(t.units.c.id, t.units.c.material_id)).all())
    assert view["status"] == "partial"
    assert (view["pages"], view["donePages"]) == (14, 5)
    assert view["from"] is view["to"] is view["unitNo"] is view["doneTo"] is None
    assert view["ranges"] == [
        {"id": row["id"], "materialId": materials[row["unit_id"]], "unitId": row["unit_id"],
         "conceptId": row["concept_id"], "from": row["start_page"], "to": row["end_page"], "doneTo": done_to}
        for row, done_to in zip(ranges, [17, None, 13])
    ]
    assert data["progress"]["totalPages"] == sum(item["pages"] for item in data["tasks"]) == 32
    assert data["progress"]["donePages"] == sum(item["donePages"] for item in data["tasks"]) == 23


def test_today_single_range_tracks_post_and_full_replacement_patch(api_client, demo_user, domain_connection):
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.commit()
    before = home_task(api_client, demo_user, task.id)
    assert (before["from"], before["to"], before["unitNo"], before["pages"], before["donePages"]) == (12, 20, 2, 9, 0)
    assert before["doneTo"] is before["ranges"][0]["doneTo"] is None
    result = post(api_client, demo_user, task, "partial", toPage=13)
    assert result.status_code == 200, result.text
    partial = home_task(api_client, demo_user, task.id)
    assert (partial["status"], partial["donePages"], partial["doneTo"]) == ("partial", 2, 13)

    for fields, expected in [({"completedRanges": []}, ("none", 0, None)), ({"toPage": 20}, ("done", 9, 20))]:
        result = api_client.patch(f"/api/tasks/{task.id}/record", headers=headers(demo_user), json=fields)
        assert result.status_code == 200, result.text
        fixed = result.json()["data"]
        view = home_task(api_client, demo_user, task.id)
        assert (view["status"], view["donePages"], view["doneTo"]) == expected
        assert {key: view[key] for key in ("ranges", "donePages", "doneTo")} == {
            key: fixed[key] for key in ("ranges", "donePages", "doneTo")}


@pytest.mark.parametrize("status", ["planned", "partial", "completed", "missed"])
def test_today_does_not_infer_completed_pages_from_status(api_client, demo_user, domain_connection, status):
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.execute(update(t.study_tasks).where(t.study_tasks.c.id == task.id).values(status=status))
    domain_connection.commit()
    view = home_task(api_client, demo_user, task.id)
    assert view["donePages"] == 0
    assert view["doneTo"] is view["ranges"][0]["doneTo"] is None


@pytest.mark.parametrize("nominal", [False, True])
def test_today_unpaged_task_uses_recorded_status_with_zero_page_counts(api_client, demo_user, domain_connection, nominal):
    task = task_for(domain_connection, demo_user, "AI2")
    if nominal:
        domain_connection.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task.id)
                                  .values(start_page=None, end_page=None, planned_minutes=25))
    else:
        domain_connection.execute(t.study_task_ranges.delete().where(t.study_task_ranges.c.task_id == task.id))
    domain_connection.commit()
    for result in ["done", "none"]:
        response = post(api_client, demo_user, task, result)
        assert response.status_code == 200, response.text
        view = home_task(api_client, demo_user, task.id)
        assert view["status"] == result
        assert view["pages"] == view["donePages"] == 0
        assert view["ranges"] == []
        assert view["from"] is view["to"] is view["unitNo"] is view["doneTo"] is None


def test_study_range_identity_survives_archived_subject_and_retired_unit(api_client, demo_user, domain_connection):
    conn = domain_connection
    task = task_for(conn, demo_user, "DB2")
    before = StudyData(conn, demo_user).task_view(task)
    conn.execute(update(t.subjects).where(t.subjects.c.id == task.row["subject_id"]).values(status="archived"))
    conn.execute(update(t.units).where(t.units.c.id == task.ranges[0]["unit_id"])
                 .values(retired_at=datetime.now(timezone.utc)))
    data = StudyData(conn, demo_user)
    historical = next(item for item in data.tasks if item.id == task.id)
    assert data.unit_of(historical) is None
    assert data.task_view(historical) == before


@pytest.mark.parametrize("foreign", [False, True])
def test_today_excludes_range_from_other_subject_or_owner(api_client, demo_user, domain_connection, foreign):
    owner = api_client.post("/api/session").json()["data"]["anonymousId"] if foreign else demo_user
    task = task_for(domain_connection, demo_user, "AI2")
    other = task_for(domain_connection, owner, "DB2")
    domain_connection.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task.id)
                              .values(unit_id=other.ranges[0]["unit_id"], concept_id=None))
    domain_connection.commit()
    view = home_task(api_client, demo_user, task.id)
    assert view["ranges"] == []
    assert view["pages"] == view["donePages"] == 0


def test_today_uses_only_this_tasks_record_snapshot(api_client, demo_user, domain_connection):
    task = task_for(domain_connection, demo_user, "AI2")
    other = task_for(domain_connection, demo_user, "DB2")
    assert other.record is not None
    domain_connection.execute(t.study_record_ranges.insert().values(
        id=str(uuid4()), record_id=other.record["id"], unit_id=task.ranges[0]["unit_id"], start_page=12, end_page=20))
    domain_connection.commit()
    view = home_task(api_client, demo_user, task.id)
    assert view["donePages"] == 0
    assert view["doneTo"] is view["ranges"][0]["doneTo"] is None


RANGE_FIELDS = ("ranges", "pages", "donePages", "from", "to", "doneTo", "unitNo")


def assert_focus_range_view(session, expected):
    assert {key: session["task"][key] for key in RANGE_FIELDS} == {key: expected[key] for key in RANGE_FIELDS}
    first = expected["ranges"][0] if expected["ranges"] else None
    assert session["material"] == ({"id": first["materialId"], "page": first["from"]} if first else None)


def test_every_focus_endpoint_keeps_shared_fields_through_response_model(api_client, clock, demo_user, domain_connection):
    task, ranges = configure_ranges(domain_connection, demo_user, [("b", 12, 16), ("a", 12, 16), ("a", 15, 20)])
    result = post(api_client, demo_user, task, "partial", completedRanges=[
        {"rangeId": ranges[0]["id"], "toPage": 16}, {"rangeId": ranges[1]["id"], "toPage": 13}])
    assert result.status_code == 200, result.text
    expected = home_task(api_client, demo_user, task.id)
    assert (expected["pages"], expected["donePages"]) == (14, 7)
    assert [item["doneTo"] for item in expected["ranges"]] == [16, 13, None]
    first = active(api_client, demo_user)
    assert_focus_range_view(first, expected)
    paths = [
        ("get", f"/api/study/sessions/{first['id']}", None, "active"),
        ("post", "/api/study/sessions", {"taskId": task.id}, "active"),
        ("post", f"/api/study/sessions/{first['id']}/pause", None, "paused"),
        ("post", f"/api/study/sessions/{first['id']}/resume", None, "active"),
        ("post", f"/api/study/sessions/{first['id']}/end", None, "ended"),
    ]
    for method, path, body, status in paths:
        response = api_client.request(method, path, headers=headers(demo_user), json=body)
        assert response.status_code == 200, response.text
        session = response.json()["data"]
        assert_focus_range_view(session, expected)
        assert (session["status"], session["task"]["minutes"], session["minutes"]) == (status, 25, 42)
        assert session["quote"] == first["quote"]


@pytest.mark.parametrize("spans,count,done_to", [([(12, 13), (17, 30)], 6, 13), ([(17, 30)], 4, None), ([(1, 30)], 9, 20)])
def test_focus_and_home_clip_snapshot_islands_without_inventing_prefix(api_client, clock, demo_user, domain_connection,
                                                                     spans, count, done_to):
    conn = domain_connection
    task = task_for(conn, demo_user, "AI2")
    record_id = str(uuid4())
    conn.execute(t.study_records.insert().values(id=record_id, task_id=task.id, result="partial",
                 recorded_at=clock[0], study_date=task.row["scheduled_date"], updated_at=clock[0]))
    for start, end in spans:
        conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record_id,
                     unit_id=task.ranges[0]["unit_id"], start_page=start, end_page=end))
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == task.id).values(status="partial"))
    conn.commit()

    expected = home_task(api_client, demo_user, task.id)
    assert (expected["pages"], expected["donePages"], expected["unitNo"]) == (9, count, 2)
    assert expected["doneTo"] == expected["ranges"][0]["doneTo"] == done_to
    assert_focus_range_view(active(api_client, demo_user), expected)


def test_focus_and_home_use_latest_record_snapshot_only(api_client, clock, demo_user, domain_connection):
    conn = domain_connection
    task = task_for(conn, demo_user, "AI2")
    for at, end in [(clock[0] - timedelta(days=1), 20), (clock[0], 13)]:
        record_id = str(uuid4())
        conn.execute(t.study_records.insert().values(id=record_id, task_id=task.id, result="partial",
                     recorded_at=at, study_date=task.row["scheduled_date"], updated_at=at))
        conn.execute(t.study_record_ranges.insert().values(id=str(uuid4()), record_id=record_id,
                     unit_id=task.ranges[0]["unit_id"], start_page=12, end_page=end))
    conn.commit()
    expected = home_task(api_client, demo_user, task.id)
    assert (expected["donePages"], expected["doneTo"]) == (2, 13)
    assert_focus_range_view(active(api_client, demo_user), expected)


@pytest.mark.parametrize("nominal", [False, True])
def test_focus_and_home_unpaged_task_returns_no_material_or_page_completion(api_client, clock, demo_user,
                                                                         domain_connection, nominal):
    task = task_for(domain_connection, demo_user, "AI2")
    if nominal:
        domain_connection.execute(update(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task.id)
                                  .values(start_page=None, end_page=None, planned_minutes=25))
    else:
        domain_connection.execute(t.study_task_ranges.delete().where(t.study_task_ranges.c.task_id == task.id))
    domain_connection.commit()
    expected = home_task(api_client, demo_user, task.id)
    session = active(api_client, demo_user)
    assert_focus_range_view(session, expected)
    assert session["status"] == "active"
    assert session["task"]["pages"] == session["task"]["donePages"] == 0
    assert session["task"]["unitNo"] is session["task"]["doneTo"] is None


def test_historical_focus_keeps_common_view_after_retirement_and_archival(api_client, clock, demo_user, domain_connection):
    task = task_for(domain_connection, demo_user, "AI2")
    domain_connection.commit()
    result = post(api_client, demo_user, task, "partial", toPage=13)
    assert result.status_code == 200, result.text
    first = active(api_client, demo_user)
    ended = action(api_client, demo_user, first["id"], "end")
    assert ended.status_code == 200, ended.text
    conn = domain_connection
    conn.execute(update(t.subjects).where(t.subjects.c.id == first["subjectId"]).values(status="archived"))
    conn.execute(update(t.units).where(t.units.c.id == task.ranges[0]["unit_id"]).values(retired_at=clock[0]))
    conn.commit()
    response = api_client.get(f"/api/study/sessions/{first['id']}", headers=headers(demo_user))
    assert response.status_code == 200, response.text
    history = response.json()["data"]
    assert_focus_range_view(history, first["task"])
    assert (history["task"]["unitNo"], history["task"]["doneTo"], history["task"]["donePages"]) == (2, 13, 2)
    assert history["status"] == "ended"
