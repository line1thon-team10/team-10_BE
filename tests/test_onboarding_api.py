from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.db import schema as t
from app.domain.onboarding import ConversationMessage, GoalUpdate
from app.domain.user import User
from app.repositories.users import UserRepository
from app.services.onboarding import OnboardingService
from tests.test_onboarding_state import ALL_CONFIRMED, NOW, add_subject, full_goal, onboarding_user


def headers(user):
    return {"X-Anonymous-Id": user["id"] if isinstance(user, dict) else user}


def test_fresh_session_returns_complete_empty_shape_without_invented_dialogue(api_client, domain_connection, monkeypatch):
    from app.routers import session
    monkeypatch.setattr(session, "DEMO_MODE", False)
    user_id = api_client.post("/api/session").json()["data"]["anonymousId"]
    response = api_client.get("/api/onboarding", headers=headers(user_id))
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"success": True, "data": {
        "messages": [], "chips": [], "subjects": [], "schedules": [], "done": False,
    }}
    assert domain_connection.execute(select(t.onboarding_messages)).first() is None
    assert domain_connection.execute(select(t.subjects)).first() is None


def test_get_restores_optional_none_confirmations_and_has_no_side_effects(api_client, domain_connection, onboarding_user, monkeypatch):
    from app.services import day_rollover

    conn, user = domain_connection, onboarding_user
    monkeypatch.setattr(day_rollover, "now_utc", lambda: NOW)
    # D04 initializes the date marker on admission. Snapshot an already-settled
    # user so the complete row comparison still detects every O01 GET mutation.
    user = day_rollover.DayRolloverService(conn).reconcile(user)
    subject_id = add_subject(conn, user)
    expected = OnboardingService(conn, user).save_turn(
        goals=[full_goal(subject_id)], messages=[ConversationMessage("user", "추가 정보는 없어요"),
                                                ConversationMessage("ai", "네 항목을 확인했어요")])
    conn.commit()
    before_subject = dict(conn.execute(select(t.subjects)).mappings().one())
    before_user = dict(conn.execute(select(t.users)).mappings().one())
    before_messages = [dict(row) for row in conn.execute(select(t.onboarding_messages)).mappings()]
    conn.commit()
    for _ in range(2):
        response = api_client.get("/api/onboarding", headers=headers(user))
        assert response.status_code == 200, response.text
        assert response.json()["data"] == expected
    assert expected["subjects"][0]["goalTotal"] is None and expected["subjects"][0]["goalEtc"] is None
    assert expected["subjects"][0]["confirmedFields"] == ALL_CONFIRMED and expected["done"]
    assert dict(conn.execute(select(t.subjects)).mappings().one()) == before_subject
    assert dict(conn.execute(select(t.users)).mappings().one()) == before_user
    assert [dict(row) for row in conn.execute(select(t.onboarding_messages)).mappings()] == before_messages
    assert conn.execute(select(t.plans)).first() is None


def test_nullable_card_and_structured_goal_details_are_returned(api_client, domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    subject_id = add_subject(conn, user, target_grade="A+", passing_criteria="60점 이상", grade_criteria="절대 평가",
                             grading_weights="중간 40%, 기말 60%", emphasized_content="트랜잭션", other_exam_info="오픈북")
    conn.commit()
    response = api_client.get("/api/onboarding", headers=headers(user))
    assert response.status_code == 200, response.text
    card = response.json()["data"]["subjects"][0]
    assert card["id"] == subject_id and card["examDate"] is None and card["target"] is None
    assert card["goalTotal"] == "60점 이상\n절대 평가"
    assert card["goalEtc"] == "중간 40%, 기말 60%\n트랜잭션\n오픈북"
    assert card["status"] == "asking" and not response.json()["data"]["done"]


def test_draft_context_restores_only_its_own_history(api_client, domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    active = add_subject(conn, user, status="active", exam_date=date(2026, 12, 1), target_grade="A+")
    first, second = add_subject(conn, user), add_subject(conn, user)
    conn.execute(update(t.users).where(t.users.c.id == user["id"]).values(onboarding_status="completed"))
    service = OnboardingService(conn, user)
    expected = service.save_turn(subject_id=first, goals=[full_goal(first)],
                                 messages=[ConversationMessage("ai", "첫 추가 과목", ("확인",))])
    service.save_turn(subject_id=second, messages=[ConversationMessage("ai", "둘째 추가 과목")])
    conn.commit()
    first_response = api_client.get("/api/onboarding", headers=headers(user), params={"subjectId": first})
    assert first_response.status_code == 200, first_response.text
    assert first_response.json()["data"] == expected
    second_response = api_client.get("/api/onboarding", headers=headers(user), params={"subjectId": second})
    assert second_response.json()["data"]["messages"] == [{"role": "ai", "text": "둘째 추가 과목"}]
    assert not second_response.json()["data"]["done"]
    initial = api_client.get("/api/onboarding", headers=headers(user)).json()["data"]
    assert [subject["id"] for subject in initial["subjects"]] == [active]
    assert initial["messages"] == [] and initial["chips"] == []


@pytest.mark.parametrize("invalid_context", ["foreign", "missing", "malformed", "empty", "active", "archived"])
def test_context_errors_do_not_expose_other_subjects(api_client, domain_connection, onboarding_user, invalid_context):
    conn, user = domain_connection, onboarding_user
    if invalid_context == "foreign":
        other = User.anonymous(now=NOW)
        UserRepository(conn).add(other)
        subject_id = add_subject(conn, other.to_mapping(), name="다른 사용자 비공개 과목")
    elif invalid_context in ("active", "archived"):
        subject_id = add_subject(conn, user, status=invalid_context)
    else:
        subject_id = {"missing": str(uuid4()), "malformed": "invalid-id", "empty": ""}[invalid_context]
    conn.commit()
    response = api_client.get("/api/onboarding", headers=headers(user), params={"subjectId": subject_id})
    expected_status, code = (409, "ONBOARDING_COMPLETED") if invalid_context == "active" else (404, "SUBJECT_NOT_FOUND")
    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == code
    assert "다른 사용자 비공개 과목" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_route_enforces_session_and_query_schema_with_real_message_endpoint(api_client, onboarding_user):
    response = api_client.get("/api/onboarding")
    assert response.status_code == 401 and response.json()["error"]["code"] == "NO_SESSION"
    response = api_client.get("/api/onboarding", headers=headers(onboarding_user), params={"unexpected": "x"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_INPUT"
    response = api_client.post("/api/onboarding/message", json={"text": "안녕하세요"})
    assert response.status_code == 401 and response.json()["error"]["code"] == "NO_SESSION"
    paths = api_client.get("/openapi.json").json()["paths"]
    assert set(paths["/api/onboarding"]) == {"get"}
    assert set(paths["/api/onboarding/message"]) == {"post"}


def test_demo_approval_does_not_invent_four_field_confirmation(api_client, domain_connection, demo_user):
    response = api_client.get("/api/onboarding", headers=headers(demo_user))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["subjects"]) == 3 and data["schedules"]
    assert not data["done"]
    assert all(subject["status"] == "asking" and not any(subject["confirmedFields"].values())
               for subject in data["subjects"])
    assert domain_connection.scalar(select(t.users.c.onboarding_status).where(t.users.c.id == demo_user)) == "completed"
    assert all(status == "active" for status in domain_connection.scalars(select(t.subjects.c.status)
                                                                        .where(t.subjects.c.user_id == demo_user)))


def test_demo_empty_seed_removes_owned_context_before_subject_fk(api_client, domain_connection, demo_user):
    conn = domain_connection
    user = dict(conn.execute(select(t.users).where(t.users.c.id == demo_user)).mappings().one())
    draft = add_subject(conn, user)
    OnboardingService(conn, user).save_turn(subject_id=draft, goals=[full_goal(draft)],
                                          messages=[ConversationMessage("ai", "추가 과목 대화")])
    other = User.anonymous(now=NOW)
    UserRepository(conn).add(other)
    other_draft = add_subject(conn, other.to_mapping())
    OnboardingService(conn, other.to_mapping()).save_turn(
        subject_id=other_draft, messages=[ConversationMessage("ai", "다른 사용자 대화")])
    conn.commit()
    response = api_client.post("/api/demo/seed", headers=headers(demo_user), json={"seed": "empty"})
    assert response.status_code == 200, response.text
    assert api_client.get("/api/onboarding", headers=headers(demo_user)).json()["data"] == {
        "messages": [], "chips": [], "subjects": [], "schedules": [], "done": False,
    }
    other_response = api_client.get("/api/onboarding", headers=headers(other.id), params={"subjectId": other_draft})
    assert other_response.json()["data"]["messages"] == [{"role": "ai", "text": "다른 사용자 대화"}]
