from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.domain.onboarding import CONFIRMATION_COLUMNS, ConversationMessage, GoalUpdate, goal_changes, subject_view
from app.domain.user import User
from app.repositories.locking import lock_user
from app.repositories.users import UserRepository
from app.services.onboarding import OnboardingService

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
ALL_CONFIRMED = {field: True for field in CONFIRMATION_COLUMNS}


@pytest.fixture
def onboarding_user(domain_connection):
    user = User.anonymous(now=NOW)
    UserRepository(domain_connection).add(user)
    domain_connection.commit()
    return user.to_mapping()


def add_subject(conn, user, **values):
    subject_id = str(uuid4())
    conn.execute(t.subjects.insert().values(**{
        "id": subject_id, "user_id": user["id"], "code": subject_id, "name": "데이터베이스", "color": "#123456",
        "created_at": NOW, "updated_at": NOW, **values,
    }))
    return subject_id


def row_for(conn, subject_id):
    return dict(conn.execute(select(t.subjects).where(t.subjects.c.id == subject_id)).mappings().one())


def full_goal(subject_id):
    return GoalUpdate(subject_id, {"exam_date": date(2026, 12, 1), "target_grade": "A+", "target_score": 90}, ALL_CONFIRMED)


def assert_error(code, action, status=None):
    with pytest.raises(ApiError) as caught:
        action()
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status


def test_empty_and_pending_subject_never_finish(domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    service = OnboardingService(conn, user)
    assert service.get() == {"messages": [], "chips": [], "subjects": [], "schedules": [], "done": False}
    subject_id = add_subject(conn, user)
    view = service.get()
    assert not view["done"]
    assert view["subjects"] == [{
        "id": subject_id, "name": "데이터베이스", "examDate": None, "goal": None,
        "goalTotal": None, "goalEtc": None, "target": None, "status": "pending",
        "confirmedFields": {field: False for field in ALL_CONFIRMED},
    }]


def test_explicit_none_and_partial_answers_survive_reconnection(domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    subject_id = add_subject(conn, user)
    service = OnboardingService(conn, user)
    view = service.save_turn(goals=[GoalUpdate(subject_id, {"exam_date": date(2026, 12, 1)}, {"examDate": True})],
                             messages=[ConversationMessage("user", "12월 1일이 시험이에요"),
                                       ConversationMessage("ai", "목표 학점은 무엇인가요?", ("A+", "B+"))])
    assert view["subjects"][0]["status"] == "asking" and not view["done"]
    assert view["subjects"][0]["confirmedFields"] == {**dict.fromkeys(ALL_CONFIRMED, False), "examDate": True}
    conn.commit()
    restored = OnboardingService(conn, UserRepository(conn).get(UUID(user["id"])).to_mapping()).get()
    assert restored == view
    complete = service.save_turn(goals=[GoalUpdate(subject_id, {"target_grade": "A+"},
                                                   {"goal": True, "goalTotal": True, "goalEtc": True})])
    subject = complete["subjects"][0]
    assert complete["done"] and subject["status"] == "confirmed"
    assert subject["goalTotal"] is None and subject["goalEtc"] is None and subject["target"] is None
    assert subject["confirmedFields"] == ALL_CONFIRMED
    saved = row_for(conn, subject_id)
    assert all(saved[column] is not None for column in CONFIRMATION_COLUMNS.values())
    assert saved["status"] == "draft"
    assert conn.scalar(select(t.users.c.onboarding_status)) == "draft"
    assert conn.execute(select(t.plans)).first() is None


def test_values_and_legacy_timestamp_do_not_imply_confirmation(domain_connection, onboarding_user):
    conn = domain_connection
    subject_id = add_subject(conn, onboarding_user, exam_date=date(2026, 12, 1), target_grade="A+",
                             passing_criteria="90점", other_exam_info="없음", goal_confirmed_at=NOW)
    view = OnboardingService(conn, onboarding_user).get()
    assert view["subjects"][0]["id"] == subject_id
    assert view["subjects"][0]["status"] == "asking" and not view["done"]
    assert not any(view["subjects"][0]["confirmedFields"].values())


def test_all_subjects_must_finish_and_order_is_stable(domain_connection, onboarding_user):
    conn = domain_connection
    later = add_subject(conn, onboarding_user, exam_date=date(2026, 12, 2))
    earlier = add_subject(conn, onboarding_user, exam_date=date(2026, 12, 1))
    uncollected = add_subject(conn, onboarding_user)
    service = OnboardingService(conn, onboarding_user)
    view = service.save_turn(goals=[full_goal(earlier)])
    assert [subject["id"] for subject in view["subjects"]] == [earlier, later, uncollected]
    assert not view["done"]
    assert [subject["status"] for subject in view["subjects"]] == ["confirmed", "asking", "pending"]


@pytest.mark.parametrize("values,group", [
    ({"exam_date": date(2026, 12, 2)}, "examDate"), ({"target_grade": "B+"}, "goal"),
    ({"target_score": 85}, "goal"), ({"passing_criteria": "80점"}, "goalTotal"),
    ({"emphasized_content": "트랜잭션"}, "goalEtc"),
])
def test_value_change_clears_only_its_confirmation(domain_connection, onboarding_user, values, group):
    conn = domain_connection
    subject_id = add_subject(conn, onboarding_user)
    service = OnboardingService(conn, onboarding_user)
    service.save_turn(goals=[full_goal(subject_id)])
    changed = service.save_turn(goals=[GoalUpdate(subject_id, values)])
    assert not changed["done"] and changed["subjects"][0]["status"] == "asking"
    assert changed["subjects"][0]["confirmedFields"] == {**ALL_CONFIRMED, group: False}
    assert row_for(conn, subject_id)["goal_confirmed_at"] is None
    reconfirmed = service.save_turn(goals=[GoalUpdate(subject_id, confirmed_fields={group: True})])
    assert reconfirmed["done"]


def test_explicit_unconfirm_and_same_value_retry_preserve_other_facts(domain_connection, onboarding_user, monkeypatch):
    from app.services import onboarding
    conn = domain_connection
    subject_id = add_subject(conn, onboarding_user)
    monkeypatch.setattr(onboarding, "now_utc", lambda: NOW)
    service = OnboardingService(conn, onboarding_user)
    first = service.save_turn(goals=[full_goal(subject_id)])
    initial = row_for(conn, subject_id)
    monkeypatch.setattr(onboarding, "now_utc", lambda: NOW + timedelta(days=1))
    assert service.save_turn(goals=[full_goal(subject_id)]) == first
    assert row_for(conn, subject_id) == initial
    invalidated = service.save_turn(goals=[GoalUpdate(subject_id, confirmed_fields={"goalEtc": False})])
    assert not invalidated["done"]
    assert invalidated["subjects"][0]["confirmedFields"] == {**ALL_CONFIRMED, "goalEtc": False}


@pytest.mark.parametrize("values,confirmations", [
    ({"exam_date": "2026-12-01"}, {}), ({"exam_date": datetime(2026, 12, 1, tzinfo=timezone.utc)}, {}),
    ({"target_grade": True}, {}), ({"status": "active"}, {}), ({}, {"missing": True}),
    ({}, {"goal": "true"}), ({}, {"examDate": True}), ({}, {"goal": True}),
    ({"target_grade": " \n"}, {"goal": True}), ({}, {}),
    ({"target_score": True}, {}), ({"target_score": "90"}, {}), ({"target_score": -1}, {}),
    ({"target_score": 101}, {}), ({"target_score": float("nan")}, {}), ({"target_score": float("inf")}, {}),
    ({"target_score": 10 ** 1000}, {}), ({"target_score": Decimal("sNaN")}, {}),
    ({"target_score": Decimal("NaN")}, {}), ({"target_score": Decimal("Infinity")}, {}),
    ({"target_score": Decimal("100.00000000000000001")}, {}),
    ({"target_score": Decimal("-1e-1000")}, {}),
])
def test_invalid_goal_updates_never_confirm(values, confirmations):
    with pytest.raises(ValueError):
        goal_changes({"id": "subject", "name": "과목"}, GoalUpdate("subject", values, confirmations), NOW)


@pytest.mark.parametrize("score", [0, 100, 89.5, Decimal("90.25")])
def test_target_accepts_finite_numeric_boundaries(score):
    changes = goal_changes({"id": "subject", "name": "과목"}, GoalUpdate("subject", {"target_score": score}), NOW)
    assert changes["target_score"] == score


@pytest.mark.parametrize("score", [Decimal("100.00000000000000001"), Decimal("-1e-1000")])
def test_decimal_rounding_cannot_save_an_out_of_range_target(domain_connection, onboarding_user, score):
    conn = domain_connection
    subject_id = add_subject(conn, onboarding_user, target_grade="A+", target_score=90)
    conn.commit()
    before = row_for(conn, subject_id)
    assert_error("INVALID_INPUT", lambda: OnboardingService(conn, onboarding_user).save_turn(
        goals=[GoalUpdate(subject_id, {"target_score": score}, {"goal": True})],
        messages=[ConversationMessage("user", "목표 확인")]), status=422)
    assert row_for(conn, subject_id) == before
    assert conn.execute(select(t.onboarding_messages.c.id)).first() is None


def test_read_requires_valid_required_values_even_if_confirmation_metadata_is_corrupt():
    row = {"id": "subject", "name": "과목", **dict.fromkeys(CONFIRMATION_COLUMNS.values(), NOW)}
    for values in ({}, {"exam_date": date(2026, 12, 1)}, {"target_grade": "A+"},
                   {"exam_date": date(2026, 12, 1), "target_grade": " \n"}):
        assert subject_view({**row, **values})["status"] == "asking"


def test_reconfirmed_change_records_new_confirmation_time():
    row = {"id": "subject", "name": "과목", "exam_date": date(2026, 12, 1), "target_grade": "A+",
           "goal_confirmed_at": NOW, **dict.fromkeys(CONFIRMATION_COLUMNS.values(), NOW)}
    later = NOW + timedelta(hours=1)
    changes = goal_changes(row, GoalUpdate("subject", {"target_grade": "B+"}, {"goal": True}), later)
    assert changes["onboarding_goal_confirmed_at"] == changes["goal_confirmed_at"] == later
    assert "onboarding_exam_date_confirmed_at" not in changes


def test_messages_preserve_turn_order_and_latest_chips(domain_connection, onboarding_user, monkeypatch):
    from app.services import onboarding
    conn, user = domain_connection, onboarding_user
    monkeypatch.setattr(onboarding, "now_utc", lambda: NOW)
    # Legacy messages have no sequence; they remain before newly saved messages.
    conn.execute(t.onboarding_messages.insert().values(
        id=str(uuid4()), user_id=user["id"], role="ai", text="기존 대화", chips=None, created_at=NOW))
    service = OnboardingService(conn, user)
    for index in range(3):
        view = service.save_turn(messages=[ConversationMessage("user", f"답변 {index}"),
                                           ConversationMessage("ai", f"질문 {index}", (f"선택 {index}",))])
    conn.commit()
    assert [message["text"] for message in view["messages"]] == ["기존 대화", "답변 0", "질문 0", "답변 1", "질문 1", "답변 2", "질문 2"]
    assert view["chips"] == ["선택 2"]
    view = OnboardingService(conn, user).save_turn(messages=[ConversationMessage("user", "선택 2")])
    assert view["chips"] == []
    assert not view["done"]


@pytest.mark.parametrize("message", [ConversationMessage("system", "답변"), ConversationMessage("user", " \n"),
    ConversationMessage("ai", "질문", ("",)), ConversationMessage("ai", "질문", (True,)),
    ConversationMessage("ai", "질문", "칩"), ConversationMessage("user", "답변", ("칩",))])
def test_bad_message_does_not_partially_save_goals(domain_connection, onboarding_user, message):
    conn = domain_connection
    subject_id = add_subject(conn, onboarding_user)
    conn.commit()
    assert_error("INVALID_INPUT", lambda: OnboardingService(conn, onboarding_user).save_turn(
        goals=[full_goal(subject_id)], messages=[message]), 422)
    assert row_for(conn, subject_id)["exam_date"] is None
    assert conn.execute(select(t.onboarding_messages)).first() is None


def test_failed_persistence_rolls_back_goals_and_messages(domain_connection, onboarding_user, monkeypatch):
    conn = domain_connection
    subject_id = add_subject(conn, onboarding_user)
    conn.commit()
    service = OnboardingService(conn, onboarding_user)
    real_append = service.repository.append_messages
    def fail(*args):
        real_append(*args)
        raise RuntimeError("failure after writes")
    monkeypatch.setattr(service.repository, "append_messages", fail)
    with pytest.raises(RuntimeError), conn.begin():
        service.save_turn(goals=[full_goal(subject_id)], messages=[ConversationMessage("user", "A+예요")])
    assert row_for(conn, subject_id)["target_grade"] is None
    assert conn.execute(select(t.onboarding_messages)).first() is None


def test_owner_and_draft_context_are_checked_before_writes(domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    other = User.anonymous(now=NOW)
    UserRepository(conn).add(other)
    foreign = add_subject(conn, other.to_mapping())
    own = add_subject(conn, user)
    active = add_subject(conn, user, status="active")
    archived = add_subject(conn, user, status="archived")
    conn.commit()
    service = OnboardingService(conn, user)
    for subject_id in (foreign, archived, str(uuid4()), "bad-id", ""):
        assert_error("SUBJECT_NOT_FOUND", lambda: service.get(subject_id), 404)
        assert_error("SUBJECT_NOT_FOUND", lambda: service.save_turn(subject_id=subject_id), 404)
    assert_error("ONBOARDING_COMPLETED", lambda: service.get(active), 409)
    assert_error("ONBOARDING_COMPLETED", lambda: service.save_turn(goals=[full_goal(active)]), 409)
    assert_error("SUBJECT_NOT_FOUND", lambda: service.save_turn(goals=[full_goal(own), full_goal(foreign)]), 404)
    assert row_for(conn, own)["exam_date"] is None
    assert service.get(own.replace("-", "").upper())["subjects"][0]["id"] == own


def test_duplicate_or_other_context_updates_cannot_mutate_rows(domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    first, second = add_subject(conn, user), add_subject(conn, user)
    service = OnboardingService(conn, user)
    assert_error("INVALID_INPUT", lambda: service.save_turn(goals=[full_goal(first), full_goal(first)]), 422)
    assert_error("SUBJECT_NOT_FOUND", lambda: service.save_turn(subject_id=first, goals=[full_goal(second)]), 404)
    assert row_for(conn, first)["exam_date"] is None and row_for(conn, second)["exam_date"] is None


def test_completed_plan_and_initial_conversation_are_separate_from_added_draft(domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    active = add_subject(conn, user)
    service = OnboardingService(conn, user)
    service.save_turn(goals=[full_goal(active)], messages=[ConversationMessage("ai", "최초 대화")])
    conn.execute(update(t.subjects).where(t.subjects.c.id == active).values(status="active"))
    conn.execute(update(t.users).where(t.users.c.id == user["id"]).values(onboarding_status="completed"))
    conn.execute(t.plans.insert().values(id=str(uuid4()), user_id=user["id"], revision=7, created_at=NOW, approved_at=NOW))
    first, second = add_subject(conn, user), add_subject(conn, user)
    conn.commit()
    before = row_for(conn, active)
    # A service created before approval must reread completed state after locking.
    assert_error("ONBOARDING_COMPLETED", lambda: service.save_turn(goals=[full_goal(first)]), 409)
    saved = service.save_turn(subject_id=first, goals=[full_goal(first)],
                              messages=[ConversationMessage("ai", "추가 과목 질문", ("없음",))])
    assert saved["done"] and [row["id"] for row in saved["subjects"]] == [first]
    assert saved["messages"] == [{"role": "ai", "text": "추가 과목 질문"}]
    assert service.get(second)["messages"] == [] and not service.get(second)["done"]
    assert service.get()["messages"] == [{"role": "ai", "text": "최초 대화"}]
    assert service.get()["done"] and [row["id"] for row in service.get()["subjects"]] == [active]
    assert row_for(conn, active) == before
    assert row_for(conn, first)["status"] == "draft"
    assert conn.scalar(select(t.plans.c.revision)) == 7
    assert conn.scalar(select(t.users.c.onboarding_status)) == "completed"


def test_legacy_proposal_and_other_user_messages_are_not_initial_history(domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    other = User.anonymous(now=NOW)
    UserRepository(conn).add(other)
    request_id = str(uuid4())
    conn.execute(t.plan_change_requests.insert().values(
        id=request_id, user_id=user["id"], kind="subject_add", status="needs_input", input_payload={},
        created_at=NOW, updated_at=NOW))
    for owner, change_request in ((other.id, None), (user["id"], request_id)):
        conn.execute(t.onboarding_messages.insert().values(
            id=str(uuid4()), user_id=owner, change_request_id=change_request, role="ai", text="별도 대화",
            chips=["비공개"], created_at=NOW))
    view = OnboardingService(conn, user).get()
    assert view["messages"] == [] and view["chips"] == []


def test_schedule_projection_filters_ownership_context_and_deleted_events(domain_connection, onboarding_user):
    conn, user = domain_connection, onboarding_user
    first, second = add_subject(conn, user), add_subject(conn, user)
    other = User.anonymous(now=NOW)
    UserRepository(conn).add(other)
    expected = []
    for values in ({"title": "주간", "recurrence_rule": "FREQ=WEEKLY;BYDAY=FR,MO", "subject_id": None},
                   {"title": "단발", "subject_id": first}, {"title": "다른 과목", "subject_id": second},
                   {"title": "삭제", "deleted_at": NOW}, {"title": "시험", "event_type": "exam"},
                   {"title": "타인", "user_id": other.id}):
        event_id = str(uuid4())
        conn.execute(t.calendar_events.insert().values(**{
            "id": event_id, "user_id": user["id"], "event_type": "class", "starts_at": NOW,
            "ends_at": NOW + timedelta(hours=1), "created_at": NOW, "updated_at": NOW, **values,
        }))
        if values["title"] in ("주간", "단발", "다른 과목"):
            expected.append(event_id)
    service = OnboardingService(conn, user)
    events = service.get()["schedules"]
    assert [event["id"] for event in events] == sorted(expected)
    weekly = next(event for event in events if event["title"] == "주간")
    assert weekly["days"] == [1, 5] and weekly["date"] is None
    single = next(event for event in events if event["title"] == "단발")
    assert single["days"] is None and single["date"] == "2026-11-14"
    assert single["start"] == "09:00" and single["end"] == "10:00" and single["added"] is False
    assert service.get(first)["schedules"] == [single]


def test_save_turn_respects_real_user_lock(api_client, domain_connection, onboarding_user):
    from app.db import session
    conn, user = domain_connection, onboarding_user
    subject_id = add_subject(conn, user)
    conn.commit()
    lock_user(conn, UUID(user["id"]))
    try:
        with session.engine.connect() as competitor:
            if competitor.dialect.name == "sqlite":
                competitor.exec_driver_sql("PRAGMA busy_timeout=20")
                competitor.commit()
            assert_error("REQUEST_IN_PROGRESS", lambda: OnboardingService(competitor, user).save_turn(
                goals=[full_goal(subject_id)], messages=[ConversationMessage("user", "A+예요")]), 409)
    finally:
        conn.rollback()
    assert row_for(conn, subject_id)["target_grade"] is None
    assert conn.execute(select(t.onboarding_messages)).first() is None
