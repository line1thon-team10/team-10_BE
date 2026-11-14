import pytest
from sqlalchemy import update

from app.db import schema as t
from tests.test_quiz_answer_api import answer_case, put, snapshot


@pytest.mark.parametrize("mutation", ["choice-range", "question-type"])
def test_actual_receipt_replays_after_metadata_change_but_unseen_input_is_validated(
        api_client, answer_case, domain_connection, mutation):
    case, conn = answer_case, domain_connection
    first = put(api_client, case, "choice", choice=1)
    assert first.status_code == 200
    change = {"choices": [{"text": "맞음"}]} if mutation == "choice-range" else {"type": "essay", "choices": None}
    conn.execute(update(t.questions).where(t.questions.c.id == case["choice"]).values(**change))
    before = snapshot(conn)
    conn.commit()
    replay = put(api_client, case, "choice", choice=1)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert snapshot(conn) == before
    # Neither the new revision nor changed confidence has a stored receipt.
    for change in ({"revision": 1}, {"confidence": "unsure"}):
        response = put(api_client, case, "choice", choice=1, **change)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INPUT"
        assert snapshot(conn) == before


def test_corrupt_receipt_cannot_replay_another_answer(api_client, answer_case, domain_connection):
    case, conn = answer_case, domain_connection
    assert put(api_client, case, "choice", choice=1).status_code == 200
    assert put(api_client, case, "choice", choice=0, revision=1).status_code == 200
    from sqlalchemy import select
    latest = conn.scalar(select(t.quiz_answer_heads.c.answer_id).where(
        t.quiz_answer_heads.c.quiz_item_id == case["choice_item"]))
    conn.execute(update(t.quiz_answer_receipts).where(
        t.quiz_answer_receipts.c.quiz_item_id == case["choice_item"],
        t.quiz_answer_receipts.c.request_revision == 0).values(response_answer_id=latest))
    before = snapshot(conn)
    conn.commit()
    response = put(api_client, case, "choice", choice=1)
    assert response.status_code == 409 and response.json()["error"]["code"] == "ANSWER_CONFLICT"
    assert snapshot(conn) == before
