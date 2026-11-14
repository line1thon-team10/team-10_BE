"""Quiz-before-learning and correction restoration use the same obligation hook."""
import pytest
from sqlalchemy import select

from app.db import schema as t
from tests.test_mastery_rules import mastery_case
from tests.test_remedial_tasks import remedial_case, submit


@pytest.mark.parametrize("initial", ["missing", "invalidated"])
def test_record_completion_after_low_quiz_creates_one_obligation(api_client, domain_connection, remedial_case, initial):
    conn, case = domain_connection, remedial_case
    task_id = conn.scalar(select(t.study_records.c.task_id).where(t.study_records.c.id == case["record"]))
    ranges = conn.execute(select(t.study_task_ranges).where(t.study_task_ranges.c.task_id == task_id)
                          .order_by(t.study_task_ranges.c.sort_order)).mappings().all()
    if initial == "missing":
        conn.execute(t.learning_passes.delete().where(t.learning_passes.c.id == case["initial"]))
    conn.commit()
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=case["headers"], json={
        "completedRanges": [{"rangeId": ranges[0]["id"], "toPage": 3}]})
    assert response.status_code == 200, response.text
    quiz_id, _, _ = submit(api_client, conn, case)
    assert list(conn.scalars(select(t.remedial_obligations.c.id))) == []
    before_history = list(conn.scalars(select(t.mastery_history.c.id)))
    conn.commit()
    body = {"completedRanges": [{"rangeId": row["id"], "toPage": row["end_page"]} for row in ranges]}
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=case["headers"], json=body)
    assert response.status_code == 200, response.text
    obligations = conn.execute(select(t.remedial_obligations)).mappings().all()
    assert len(obligations) == 1 and obligations[0]["quiz_session_id"] == quiz_id
    passes = conn.execute(select(t.learning_passes)).mappings().all()
    assert len(passes) == 1 and passes[0]["invalidated_at"] is None
    if initial == "invalidated":
        assert passes[0]["id"] == case["initial"]
    assert list(conn.scalars(select(t.mastery_history.c.id))) == before_history
    conn.commit()
    response = api_client.patch(f"/api/tasks/{task_id}/record", headers=case["headers"], json=body)
    assert response.status_code == 200, response.text
    assert list(conn.scalars(select(t.remedial_obligations.c.id))) == [obligations[0]["id"]]
