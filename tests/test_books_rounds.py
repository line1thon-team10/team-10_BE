from uuid import uuid4

from sqlalchemy import update

from app.db import schema as t
from tests.test_learning_evidence import learning_pass, patch
from tests.test_record_transactions import headers, task_for


def unit_view(client, user, name="데이터베이스", no=2):
    response = client.get("/api/books", headers=headers(user))
    assert response.status_code == 200, response.text
    subject = next(row for row in response.json()["data"]["subjects"] if row["name"] == name)
    return next(row for row in subject["units"] if row["no"] == no)


def test_round_counts_valid_rows_not_highest_history_number(api_client, demo_user, domain_connection):
    conn = domain_connection
    task = task_for(conn, demo_user)
    old = learning_pass(conn, task.ranges[0]["unit_id"])
    conn.execute(update(t.learning_passes).where(t.learning_passes.c.id == old["id"])
                 .values(invalidated_at=old["completed_at"]))
    for number in (2, 3, 5):
        conn.execute(t.learning_passes.insert().values(
            **{**old, "id": str(uuid4()), "pass_number": number, "completion_kind": "full_relearn",
               "evidence_key": str(uuid4()), "invalidated_at": old["completed_at"] if number == 5 else None}))
    conn.commit()
    assert unit_view(api_client, demo_user)["round"] == 2
    assert unit_view(api_client, demo_user, no=3)["round"] == 0
    other = api_client.post("/api/session").json()["data"]["anonymousId"]
    assert unit_view(api_client, other)["round"] == 1


def test_books_reflects_invalidated_and_restored_evidence(api_client, demo_user, domain_connection):
    task = task_for(domain_connection, demo_user)
    domain_connection.commit()
    assert unit_view(api_client, demo_user)["round"] == 1
    patch(api_client, demo_user, task, 13)
    partial = unit_view(api_client, demo_user)
    assert partial["round"] == 0
    assert partial["blocks"][0]["state"] == "doing"
    patch(api_client, demo_user, task, 20)
    complete = unit_view(api_client, demo_user)
    assert complete["round"] == 1
    assert complete["blocks"][0]["state"] == "done"
