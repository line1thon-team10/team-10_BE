from sqlalchemy import select

from app.db import schema as t


def test_day9_focus_record_per_item_save_submit_and_reopen(api_client, demo_user, domain_connection):
    headers = {"X-Anonymous-Id": demo_user}
    active = api_client.get("/api/study/sessions/active", headers=headers).json()["data"]
    ended = api_client.post(f"/api/study/sessions/{active['id']}/end", headers=headers)
    assert ended.status_code == 200, ended.text
    recorded = api_client.post("/api/records", headers=headers,
        json={"taskId": active["taskId"], "sessionId": active["id"], "result": "partial", "toPage": 13})
    assert recorded.status_code == 200, recorded.text
    opened = api_client.get(f"/api/quiz/{active['subjectId']}", headers=headers)
    assert opened.status_code == 200, opened.text
    quiz_id = opened.json()["data"]["quizId"]
    rows = domain_connection.execute(select(t.questions, t.quiz_items.c.id.label("item_id"))
        .join(t.quiz_items, t.quiz_items.c.question_id == t.questions.c.id)
        .where(t.quiz_items.c.quiz_session_id == quiz_id).order_by(t.quiz_items.c.sort_order)).mappings().all()
    answers = [{"questionId": row["item_id"], "confidence": "sure",
        **({"choice": next(index for index, value in enumerate(row["choices"]) if value["text"] == row["correct_answer"])}
           if row["choices"] else {"text": "" if row["type"] == "essay" else row["correct_answer"]})} for row in rows]
    domain_connection.commit()
    for answer in answers:
        saved = api_client.put(f"/api/quiz/{quiz_id}/answers/{answer['questionId']}", headers=headers,
            json={**{key: value for key, value in answer.items() if key != "questionId"}, "revision": 0})
        assert saved.status_code == 200, saved.text
    reopened = api_client.get(f"/api/quiz/{active['subjectId']}", headers=headers, params={"quizId": quiz_id})
    assert len(reopened.json()["data"]["savedAnswers"]) == len(answers)
    submitted = api_client.post(f"/api/quiz/{active['subjectId']}/submit", headers=headers,
        json={"quizId": quiz_id, "answers": answers})
    assert submitted.status_code == 200, submitted.text
    result = api_client.get(f"/api/quiz/{active['subjectId']}/result", headers=headers, params={"quizId": quiz_id})
    assert result.status_code == 200 and result.json() == submitted.json()
    assert result.json()["data"]["bookChanges"]
