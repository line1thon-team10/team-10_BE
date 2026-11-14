import json

import pytest

from app.core.responses import ApiError
from app.repositories.quiz_answers import QuizAnswerRepository
from tests.test_quiz_answer_api import DAY, NOW, answer_case, put, snapshot


@pytest.mark.parametrize("value", ["a\x00b", "\ud800", "\udfff", "ok\ud800bad"])
def test_unstorable_text_rejected_before_http_or_repository_writes(api_client, answer_case, domain_connection, value):
    case, conn = answer_case, domain_connection
    before = snapshot(conn)
    conn.commit()
    response = api_client.put(f"/api/quiz/{case['quiz']}/answers/{case['essay_item']}",
        headers={"X-Anonymous-Id": case["user"], "content-type": "application/json"},
        content=json.dumps({"text": value, "confidence": "sure", "revision": 0}))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert response.headers["cache-control"] == "no-store"
    assert snapshot(conn) == before
    with pytest.raises(ApiError) as caught:
        QuizAnswerRepository(conn).save(case["user"], case["quiz"], case["essay_item"],
            revision=0, text=value, confidence="sure", now=NOW, answered_date=DAY)
    assert (caught.value.status, caught.value.code) == (422, "INVALID_INPUT")
    assert snapshot(conn) == before


def test_valid_unicode_and_control_whitespace_preserve_exact_input(api_client, answer_case, domain_connection):
    original = "  한글😀𐐀\t\n"
    assert put(api_client, answer_case, text=original).status_code == 200
    response = api_client.get(f"/api/quiz/{answer_case['quiz']}", headers={"X-Anonymous-Id": answer_case["user"]})
    assert response.status_code == 200
    assert response.json()["data"]["savedAnswers"][0]["text"] == original
