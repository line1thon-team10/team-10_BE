"""Onboarding state and atomic persistence boundary for the later O-03 parser."""
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Connection

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.domain.onboarding import ConversationMessage, GoalUpdate, event_view, goal_changes, subject_view
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.onboarding import OnboardingRepository


class OnboardingService:
    def __init__(self, connection: Connection, user: dict):
        self.connection = connection
        self.user = user
        self.repository = OnboardingRepository(connection, user["id"])

    def _draft(self, subject_id: str) -> dict:
        try:
            subject_id = str(UUID(str(subject_id)))
        except (ValueError, TypeError, AttributeError):
            raise ApiError("SUBJECT_NOT_FOUND", "과목을 찾지 못했어요", 404) from None
        row = self.repository.subject(subject_id)
        if row is None or row["status"] == "archived":
            raise ApiError("SUBJECT_NOT_FOUND", "과목을 찾지 못했어요", 404)
        if row["status"] != "draft":
            raise ApiError("ONBOARDING_COMPLETED", "이미 승인된 과목이에요. 관리 화면에서 변경해 주세요", 409)
        return row

    def get(self, subject_id: str | None = None) -> dict:
        if subject_id is not None:
            rows = [self._draft(subject_id)]
            subject_id = rows[0]["id"]
        else:
            rows = self.repository.subjects("active" if self.user["onboarding_status"] == "completed" else "draft")
        messages = self.repository.messages(subject_id)
        subjects = [subject_view(row) for row in rows]
        events = self.repository.events([row["id"] for row in rows], include_global=subject_id is None)
        return {
            "messages": [{"role": message["role"], "text": message["text"]} for message in messages],
            "chips": (messages[-1]["chips"] or []) if messages and messages[-1]["role"] == "ai" else [],
            "subjects": subjects,
            "schedules": [event_view(event, self.user["timezone"]) for event in events],
            "done": bool(subjects) and all(subject["status"] == "confirmed" for subject in subjects),
        }

    def save_turn(self, *, subject_id: str | None = None, goals: Sequence[GoalUpdate] = (),
                  messages: Sequence[ConversationMessage] = ()) -> dict:
        """Save an interpreted turn, without committing or invoking an AI model.

        The caller runs interpretation outside this transaction. O-03 must also
        verify its interpretation's input version after acquiring this boundary;
        this method alone does not provide external-call freshness/idempotency.
        """
        try:
            fresh = lock_user(self.connection, UUID(self.user["id"]))
        except UserBusyError as exc:
            raise ApiError("REQUEST_IN_PROGRESS", "다른 요청이 처리 중이에요. 잠시 후 다시 시도해 주세요", 409) from exc
        if fresh is None:
            raise ApiError("UNKNOWN_SESSION", "사용자를 찾지 못했어요", 401)
        self.user = fresh.to_mapping()
        if subject_id is not None:
            subject_id = self._draft(subject_id)["id"]
        elif self.user["onboarding_status"] == "completed":
            raise ApiError("ONBOARDING_COMPLETED", "초기 온보딩이 끝났어요. 추가할 과목을 선택해 주세요", 409)
        at = now_utc()
        updates, seen = [], set()
        for goal in goals:
            row = self._draft(goal.subject_id)
            if subject_id is not None and row["id"] != subject_id:
                raise ApiError("SUBJECT_NOT_FOUND", "이 온보딩의 과목을 찾지 못했어요", 404)
            if row["id"] in seen:
                raise ApiError("INVALID_INPUT", "한 과목의 목표 변경은 하나로 보내 주세요", 422)
            seen.add(row["id"])
            try:
                updates.append((row["id"], goal_changes(row, goal, at)))
            except ValueError as exc:
                raise ApiError("INVALID_INPUT", "목표와 확인 상태의 형식이 맞지 않아요", 422) from exc
        try:
            saved_messages = [message.validated() for message in messages]
        except ValueError as exc:
            raise ApiError("INVALID_INPUT", "대화 내용의 형식이 맞지 않아요", 422) from exc
        # Validate the entire turn before the first domain write. The request's
        # outer transaction also rolls back failures while serializing the view.
        for target, values in updates:
            self.repository.update_goal(target, values)
        self.repository.append_messages(subject_id, saved_messages, at)
        return self.get(subject_id)
