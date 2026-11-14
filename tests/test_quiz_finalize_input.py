import json

import pytest

from app.schemas.quiz import Answer
from tests.test_quiz_answer_api import answer_case, snapshot


@pytest.mark.parametrize("text", ["a\x00b", "\ud800", "\udfff"])
def test_bulk_submit_rejects_unstorable_text_before_any_writes(api_client, answer_case, domain_connection, text):
    case, conn = answer_case, domain_connection
    answers = [{"questionId": case[f"{kind}_item"], "confidence": "sure",
                **({"choice": 0} if kind in ("choice", "ox") else {"text": text})}
               for kind in ("choice", "ox", "short", "essay")]
    before = snapshot(conn)
    conn.commit()
    response = api_client.post(f"/api/quiz/{case['quiz']}/submit",
        headers={"X-Anonymous-Id": case["user"], "content-type": "application/json"},
        content=json.dumps({"quizId": case["quiz"], "answers": answers}))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert snapshot(conn) == before


def test_bulk_input_preserves_unicode_and_whitespace():
    value = "  한글😀 e\u0301\t\r\n"
    assert Answer(questionId="00000000-0000-0000-0000-000000000001", text=value, confidence="sure").text == value
