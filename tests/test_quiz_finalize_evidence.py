import pytest
from sqlalchemy import select, update

from app.db import schema as t
from tests.test_quiz_answer_api import answer_case
from tests.test_quiz_finalize import get_result, submit


@pytest.mark.parametrize("change", [{}, {"start_page": 0}, {"end_page": 100}, {"quote": "  "}])
def test_frozen_evidence_uses_only_real_pages_and_quote_without_guessed_rectangles(
        api_client, answer_case, domain_connection, change):
    case, conn = answer_case, domain_connection
    conn.execute(update(t.question_sources).where(t.question_sources.c.question_id == case["choice"])
        .values(highlight={"unknownSchema": [{"x": -1, "width": 999}]}, **change))
    conn.commit()
    response = submit(api_client, case)
    assert response.status_code == 200, response.text
    item = next(item for item in response.json()["data"]["items"] if item["questionId"] == case["choice_item"])
    if change:
        assert item["evidence"] is None and item["page"] is None
    else:
        assert item["evidence"] == {"materialId": case["material"], "page": 1, "quote": "비공개 근거", "rects": []}
        assert item["page"] == "p.1"
    conn.execute(update(t.question_sources).where(t.question_sources.c.question_id == case["choice"])
        .values(quote="나중 바뀐 인용", start_page=2, end_page=2))
    conn.commit()
    assert get_result(api_client, case).json() == response.json()
