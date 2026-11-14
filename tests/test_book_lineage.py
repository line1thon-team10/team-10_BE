from datetime import timedelta
from uuid import uuid4
from uuid import UUID

from sqlalchemy import select, update

from app.db import schema as t
from tests.test_initial_pass import placement, record
from tests.test_record_transactions import headers, task_for


def books(client, user):
    response = client.get("/api/books", headers=headers(user))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def unit(client, user, unit_id):
    return next(unit for subject in books(client, user)["subjects"] for unit in subject["units"]
                if unit["id"] == unit_id)


def recent(client, user):
    response = client.get("/api/today", headers=headers(user))
    assert response.status_code == 200, response.text
    return response.json()["data"]["recentDays"]


def test_carried_tail_counts_one_book_and_correction_reopens_it(api_client, demo_user, domain_connection):
    conn = domain_connection
    root = task_for(conn, demo_user)
    uid, bid = root.ranges[0]["unit_id"], root.row["block_id"]
    conn.commit()
    response = api_client.patch(f"/api/tasks/{root.id}/record", headers=headers(demo_user), json={"toPage": 13})
    assert response.status_code == 200
    conn.execute(update(t.study_tasks).where(t.study_tasks.c.id == root.id).values(status="carried_over"))
    tail = placement(conn, root, 14, 20, offset=1, block_id=bid, origin=root.id)
    conn.execute(update(t.users).where(t.users.c.id == demo_user).values(
        demo_date=root.row["scheduled_date"] + timedelta(days=1)))
    conn.commit()
    record(api_client, demo_user, tail)
    view = unit(api_client, demo_user, uid)
    assert [block["id"] for block in view["blocks"]] == [bid]
    assert view["materialId"] == root.ranges[0]["material_id"]
    block = view["blocks"][0]
    assert block["state"] == "done"
    assert block["task"]["id"] == tail
    assert block["task"]["pages"] == 7
    assert block["task"]["ranges"][0]["unitId"] == uid
    days = recent(api_client, demo_user)
    assert next(b for b in days[-2]["blocks"] if b["id"] == bid)["state"] == "doing"
    assert next(b for b in days[-1]["blocks"] if b["id"] == bid)["state"] == "done"
    response = api_client.patch(f"/api/tasks/{root.id}/record", headers=headers(demo_user), json={"toPage": 12})
    assert response.status_code == 200
    assert unit(api_client, demo_user, uid)["blocks"][0]["state"] == "doing"
    assert next(b for b in recent(api_client, demo_user)[-1]["blocks"] if b["id"] == bid)["state"] == "doing"


def test_multiple_completed_placements_and_retired_unit_keep_one_identity(api_client, demo_user, domain_connection):
    conn = domain_connection
    root = task_for(conn, demo_user)
    uid, bid = root.ranges[0]["unit_id"], root.row["block_id"]
    # Legacy duplicate placements must not turn one durable block into two books.
    second = placement(conn, root, 12, 20, block_id=bid, origin=root.id)
    conn.execute(update(t.units).where(t.units.c.id == uid).values(retired_at=root.row["created_at"]))
    conn.commit()
    record(api_client, demo_user, second)
    result = unit(api_client, demo_user, uid)
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["id"] == bid
    assert result["round"] == 1
    assert len([b for b in recent(api_client, demo_user)[-1]["blocks"] if b["id"] == bid]) == 1


def test_compound_ranges_have_no_fake_linear_bounds(api_client, demo_user, domain_connection):
    conn = domain_connection
    root = task_for(conn, demo_user)
    uid = root.ranges[0]["unit_id"]
    conn.execute(t.study_task_ranges.insert().values(id=str(uuid4()), task_id=root.id, unit_id=uid,
                                                    start_page=12, end_page=13, sort_order=2))
    conn.execute(update(t.units).where(t.units.c.id == uid).values(code="NO_NUMBER", sort_order=55))
    conn.commit()
    result = unit(api_client, demo_user, uid)
    assert result["no"] == 55
    task = result["blocks"][0]["task"]
    assert task["pages"] == 9
    assert task["from"] is task["to"] is task["doneTo"] is None
    assert len(task["ranges"]) == 2


def test_today_grade_requires_finalized_quiz_for_this_unit(api_client, demo_user, domain_connection):
    conn = domain_connection
    root = task_for(conn, demo_user)
    uid = root.ranges[0]["unit_id"]
    conn.execute(update(t.units).where(t.units.c.id == uid).values(mastery_level="stable", mastery_score=90))
    other_unit = conn.scalar(select(t.units.c.id).where(
        t.units.c.material_id == root.ranges[0]["material_id"], t.units.c.id != uid))
    foreign_question = conn.scalar(select(t.questions.c.id).where(t.questions.c.unit_id == other_unit))
    own_question = conn.scalar(select(t.questions.c.id).where(t.questions.c.unit_id == uid))
    qid, item_id = str(uuid4()), str(uuid4())
    conn.execute(t.quiz_sessions.insert().values(
        id=qid, user_id=demo_user, subject_id=root.row["subject_id"], type="daily",
        quiz_date=root.record["study_date"], status="submitted", grading_status="completed",
        created_at=root.row["created_at"], finalized_at=root.row["created_at"]))
    conn.execute(t.quiz_items.insert().values(id=item_id, quiz_session_id=qid,
        question_id=foreign_question, sort_order=1, inclusion_reason="new"))
    conn.commit()
    assert unit(api_client, demo_user, uid)["blocks"][0]["state"] == "done"
    conn.execute(update(t.quiz_items).where(t.quiz_items.c.id == item_id).values(question_id=own_question))
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == qid).values(finalized_at=None))
    conn.commit()
    assert unit(api_client, demo_user, uid)["blocks"][0]["state"] == "done"
    conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == qid).values(finalized_at=root.row["created_at"]))
    conn.commit()
    assert unit(api_client, demo_user, uid)["blocks"][0]["state"] == "stable"
    assert next(b for b in recent(api_client, demo_user)[-1]["blocks"]
                if b["id"] == root.row["block_id"])["state"] == "stable"


def test_same_number_and_pages_in_two_materials_remain_separate(api_client, demo_user, domain_connection):
    from app.services.study import Task
    conn = domain_connection
    root = task_for(conn, demo_user)
    old_unit = dict(conn.execute(select(t.units).where(t.units.c.id == root.ranges[0]["unit_id"])).mappings().one())
    old_material = dict(conn.execute(select(t.materials).where(t.materials.c.id == old_unit["material_id"])).mappings().one())
    material_id, unit_id = str(uuid4()), str(uuid4())
    conn.execute(t.materials.insert().values(**{**old_material, "id": material_id, "sha256": uuid4().hex * 2}))
    conn.execute(t.units.insert().values(**{**old_unit, "id": unit_id, "material_id": material_id}))
    new_template = Task(dict(root.row), [{**root.ranges[0], "unit_id": unit_id, "material_id": material_id}])
    new_task = placement(conn, new_template, 12, 20)
    conn.commit()
    record(api_client, demo_user, new_task)
    subject = next(s for s in books(api_client, demo_user)["subjects"] if s["subjectId"] == root.row["subject_id"])
    pair = [u for u in subject["units"] if u["id"] in (old_unit["id"], unit_id)]
    assert len(pair) == 2 and pair[0]["no"] == pair[1]["no"]
    assert {u["materialId"] for u in pair} == {old_material["id"], material_id}
    assert len({u["blocks"][0]["id"] for u in pair}) == 2
    assert all(u["blocks"][0]["task"]["pages"] == 9 for u in pair)


def test_direct_review_book_is_discoverable_without_daily_plan_growth(api_client, demo_user, domain_connection):
    from tests.test_planless_review_correction import direct_record
    conn = domain_connection
    before = api_client.get("/api/today", headers=headers(demo_user)).json()["data"]
    template, task_id, _, _ = direct_record(conn, demo_user)
    uid = template.ranges[0]["unit_id"]
    conn.commit()
    direct = next(b for b in unit(api_client, demo_user, uid)["blocks"] if b["task"]["id"] == task_id)
    assert direct["state"] == "done" and direct["task"]["pages"] == 9
    assert next(b for b in recent(api_client, demo_user)[-1]["blocks"] if b["id"] == direct["id"])["taskId"] == task_id
    after = api_client.get("/api/today", headers=headers(demo_user)).json()["data"]
    assert after["tasks"] == before["tasks"]
    assert after["progress"] == before["progress"]
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=headers(demo_user), json={"toPage": 13})
    assert response.status_code == 200
    fixed = next(b for b in unit(api_client, demo_user, uid)["blocks"] if b["id"] == direct["id"])
    assert fixed["state"] == "doing"
    assert fixed["task"]["donePages"] == 2


def test_same_timestamp_completed_lineage_prefers_descendant(api_client, demo_user, domain_connection, monkeypatch):
    from tests import test_initial_pass
    conn = domain_connection
    root = task_for(conn, demo_user)
    values = iter([UUID(int=1), UUID(int=2)])
    with monkeypatch.context() as patch:
        patch.setattr(test_initial_pass, "uuid4", lambda: next(values))
        tail = placement(conn, root, 12, 20, block_id=root.row["block_id"], origin=root.id)
    conn.commit()
    record(api_client, demo_user, tail)
    book = unit(api_client, demo_user, root.ranges[0]["unit_id"])["blocks"][0]
    assert book["task"]["id"] == tail
