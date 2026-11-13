import hashlib
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db import schema as t
from app.repositories.users import UserRepository
from app.seed import package
from app.services.quiz import QuizService
from app.services.study import StudyData


def auth(user):
    return {"X-Anonymous-Id": user}


def test_package_matches_pdfs_and_question_ranges():
    subjects, quiz, mastery = package.load_package()
    assert [s["code"] for s in subjects] == ["AI", "DB", "OS"]
    assert len(quiz["units"]) == 9 and len(quiz["concepts"]) == 81 and len(quiz["questions"]) == 45
    for subject in subjects:
        pdf = package.AI_ENGINE_PATH / "data/demo" / subject["file"][0]
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == subject["sha256"]
        assert subject["file"][1] == 30
    for unit in quiz["units"]:
        questions = [q for q in quiz["questions"] if q["unit"] == unit["id"]]
        assert len(questions) == 5 and all(q["difficulty"] == "하" for q in questions)
        assert all(unit["page_start"] <= q["page"] <= unit["page_end"] for q in questions)
        assert all(q["quote"] and q["answer"] for q in questions)
    assert mastery == {"AI1": (90, "stable", False), "DB1": (40, "risk", True), "OS1": (80, "stable", False)}
    assert {code: len(ids) for code, ids in quiz["pending_quizzes_1109"].items()} == {"AI": 6, "DB": 8, "OS": 6}


def test_package_state_metrics_and_learning_evidence(api_client, demo_user, domain_connection):
    conn = domain_connection
    data = StudyData(conn, demo_user)
    assert [(s["name"], s["exam_date"]) for s in data.subjects] == [
        ("인공지능", date(2026,11,12)), ("데이터베이스", date(2026,11,16)), ("운영체제", date(2026,11,18))]
    assert {s["code"]: data.readiness(s)["readiness"] for s in data.subjects} == {"AI":30,"DB":27,"OS":53}
    tasks = data.tasks_on(date(2026,11,9))
    assert [(x.first,x.last,x.row["estimated_minutes"]) for x in tasks] == [(12,20,25)] * 3
    assert data.progress(date(2026,11,9))["donePages"] == 18
    user = UserRepository(conn).get(UUID(demo_user))
    metrics = package.engine.module("learning_metrics").calculate_learning_metrics(
        QuizService(conn,user.to_mapping(),date(2026,11,9))._metrics_input())
    stored = {u["code"]:u["mastery_score"] for u in data.units.values() if u["mastery_score"] is not None}
    assert stored == {u["unit"]:u["score"] for u in metrics["units"]}
    records = set(conn.scalars(select(t.study_records.c.id)))
    passes = conn.execute(select(t.learning_passes)).mappings().all()
    assert len(passes) == 5
    assert all(p["evidence_snapshot"]["recordIds"] and set(p["evidence_snapshot"]["recordIds"]) <= records for p in passes)
    assert {x.row["subject_id"] for x in tasks} == {s["id"] for s in data.subjects}
    assert conn.scalar(select(func.count()).select_from(t.questions)) == 45


def test_focus_end_partial_record_crosses_threshold(api_client, demo_user, end_focus):
    headers = auth(demo_user)
    before = api_client.get("/api/today",headers=headers).json()["data"]
    assert before["progress"] == {"doneCount":2,"totalCount":3,"donePages":18,"totalPages":27,"rate":67}
    assert before["quiz"]["open"] is False
    active = api_client.get("/api/study/sessions/active",headers=headers).json()["data"]
    end_focus(demo_user)
    response = api_client.post("/api/records",headers=headers,json={
        "taskId":active["taskId"],"sessionId":active["id"],"result":"partial","toPage":13})
    assert response.status_code == 200, response.text
    event = response.json()["data"]["event"]
    assert event["type"] == "box" and event["reason"] == "dayClose" and event["rate"] == 74
    after = api_client.get("/api/today",headers=headers).json()["data"]
    assert after["progress"]["donePages"] == 20 and after["quiz"]["open"] is True
    subject = next(s for s in after["subjects"] if s["name"] == "인공지능")
    quiz = api_client.get(f"/api/quiz/{subject['id']}",headers=headers)
    assert quiz.status_code == 200 and len(quiz.json()["data"]["questions"]) == 6
    assert any(q["type"] == "essay" for q in quiz.json()["data"]["questions"])


def test_overdue_has_no_focus_and_requests_replan(api_client, demo_user):
    headers = auth(demo_user)
    assert api_client.post("/api/demo/seed",headers=headers,json={"seed":"overdue"}).status_code == 200
    today = api_client.get("/api/today",headers=headers).json()["data"]
    assert today["progress"]["donePages"] == 9 and today["progress"]["totalPages"] == 27
    assert api_client.get("/api/study/sessions/active",headers=headers).json()["data"] is None
    response = api_client.post("/api/demo/next-day",headers=headers)
    assert response.status_code == 200 and response.json()["data"]["judgment"] == "replan"


def test_missing_package_rolls_back_reset(api_client, demo_user, domain_connection, monkeypatch, tmp_path):
    headers = auth(demo_user)
    before = api_client.get("/api/today",headers=headers).json()
    ids = set(domain_connection.scalars(select(t.study_sessions.c.id)))
    domain_connection.commit()
    monkeypatch.setattr(package, "AI_ENGINE_PATH", tmp_path)
    response = api_client.post("/api/demo/reset",headers=headers)
    assert response.status_code == 500 and response.json()["error"]["code"] == "SERVER_ERROR"
    assert api_client.get("/api/today",headers=headers).json() == before
    assert set(domain_connection.scalars(select(t.study_sessions.c.id))) == ids
    # empty는 AI 파일 없이도 초기화할 수 있어야 한다.
    domain_connection.commit()
    assert api_client.post("/api/demo/seed",headers=headers,json={"seed":"empty"}).status_code == 200


@pytest.mark.parametrize("kind", ["pages", "question_range", "unit_range"])
def test_corrupt_package_is_rejected(monkeypatch, tmp_path, kind):
    import json
    import shutil
    root = tmp_path / "data"
    shutil.copytree(package.AI_ENGINE_PATH / "data", root)
    monkeypatch.setattr(package,"AI_ENGINE_PATH",tmp_path)
    if kind == "question_range":
        bad = dict(package.QUESTIONS)
        bad["AI1"] = [(31, "OX", "bad", "O", ["O", "X"])]
        monkeypatch.setattr(package,"QUESTIONS",bad)
    else:
        path = root / "ground_truth/artificial_intelligence.json"
        meta = json.loads(path.read_text())
        if kind == "pages":
            meta["total_pages"] = 31
        else:
            meta["sections"][0]["start_page"] = 2
        path.write_text(json.dumps(meta))
    from app.core.responses import ApiError
    with pytest.raises(ApiError) as error:
        package.load_package()
    assert error.value.code == "DEMO_PACKAGE_INVALID"


@pytest.mark.parametrize("minutes,check,stopped", [(7,False,False),(8,True,False),(17,True,False),(18,False,True)])
def test_new_task_estimate_keeps_focus_check_and_auto_stop_rules(api_client, monkeypatch, minutes, check, stopped):
    from datetime import datetime, timedelta, timezone
    from app.seed import loader
    from app.services import focus
    from app.routers import session
    at = datetime(2030,1,1,tzinfo=timezone.utc)
    monkeypatch.setattr(loader,"now_utc",lambda: at)
    monkeypatch.setattr(focus,"now",lambda: at)
    monkeypatch.setattr(session,"DEMO_MODE",True)
    user = api_client.post("/api/session").json()["data"]["anonymousId"]
    active = api_client.get("/api/study/sessions/active",headers=auth(user)).json()["data"]
    monkeypatch.setattr(focus,"now",lambda: at + timedelta(minutes=minutes))
    result = api_client.get(f"/api/study/sessions/{active['id']}",headers=auth(user)).json()["data"]
    assert result["checkIn"] is check and result["autoStopped"] is stopped
    assert result["minutes"] == (50 if stopped else 42 + minutes)
    assert result["excludeFromPace"] is stopped
