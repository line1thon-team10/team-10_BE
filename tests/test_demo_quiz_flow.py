from sqlalchemy import select

from app.db import schema as t
from app.seed.essay_examples import DEMO_ESSAY
from app.services import quiz


def test_keyless_demo_grading_returns_saved_feedback_and_book_changes(api_client, demo_user, domain_connection, end_focus, monkeypatch):
    headers = {"X-Anonymous-Id": demo_user}
    active = api_client.get("/api/study/sessions/active",headers=headers).json()["data"]
    end_focus(demo_user)
    assert api_client.post("/api/records",headers=headers,json={"taskId":active["taskId"],
        "sessionId":active["id"],"result":"partial","toPage":13}).status_code == 200
    subject = active["subjectId"]
    response = api_client.get(f"/api/quiz/{subject}",headers=headers)
    assert response.status_code == 200
    ids = [q["id"] for q in response.json()["data"]["questions"]]
    rows = domain_connection.execute(select(t.questions).where(t.questions.c.id.in_(ids))).mappings().all()
    answers = []
    for q in rows:
        item = {"questionId":q["id"],"confidence":"sure"}
        if q["choices"]:
            item["choice"] = next(i for i,c in enumerate(q["choices"]) if c["text"] == q["correct_answer"])
        else:
            item["text"] = DEMO_ESSAY["answer"] if q["type"] == "essay" else q["correct_answer"]
        answers.append(item)
    domain_connection.commit()
    real = quiz.ai.module
    def offline(name):
        if name == "essay_grader":
            raise RuntimeError("no demo API key")
        return real(name)
    monkeypatch.setattr(quiz.ai,"module",offline)
    response = api_client.post(f"/api/quiz/{subject}/submit",headers=headers,json={"answers":answers})
    assert response.status_code == 200,response.text
    result = response.json()["data"]
    assert result["essayGrading"] == "saved" and result["bookChanges"]
    essay = next(q for q in result["items"] if q["type"] == "essay")
    assert essay["score"] == 70 and essay["feedback"]["good"] and essay["feedback"]["miss"]
    assert essay["evidence"]["page"] == 16 and essay["evidence"]["quote"]
    assert result["summary"]["total"] == 6 and result["summary"]["correct"] == 5
