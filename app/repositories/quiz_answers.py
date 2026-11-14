"""Durable quiz storage primitives. The caller owns the outer transaction."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import Connection, null, select, update

from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import UserBusyError, lock_user


def normalized_uuid(value) -> str:
    try:
        return str(value if isinstance(value, UUID) else UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ApiError("INVALID_INPUT", "식별자 형식이 맞지 않아요", 422) from None


def aware_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ApiError("INVALID_INPUT", "시간대가 있는 시각이 필요해요", 422)
    return value.astimezone(timezone.utc)


def row_dict(row) -> dict | None:
    if row is None:
        return None
    return {key: value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value
            for key, value in dict(row).items()}


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def answer_payload(*, choice: int | None, text: str | None, confidence: str) -> dict:
    if confidence not in ("sure", "unsure") or not isinstance(confidence, str):
        raise ApiError("INVALID_INPUT", "확신도는 확실/애매 중에서 골라 주세요", 422)
    if (choice is not None and (type(choice) is not int or choice < 0)) or (
            text is not None and not isinstance(text, str)) or ((choice is None) == (text is None)):
        raise ApiError("INVALID_INPUT", "문항 유형에 맞는 답안을 보내 주세요", 422)
    return {"choice": choice, "text": text, "confidence": confidence}


@dataclass(frozen=True)
class AnswerSave:
    answer: dict
    created: bool
    is_current: bool


class QuizAnswerRepository:
    def __init__(self, connection: Connection):
        self.connection = connection

    def quiz(self, user_id, quiz_id) -> dict:
        user_id, quiz_id = normalized_uuid(user_id), normalized_uuid(quiz_id)
        row = self.connection.execute(select(t.quiz_sessions).outerjoin(
            t.subjects, t.quiz_sessions.c.subject_id == t.subjects.c.id).where(
            t.quiz_sessions.c.id == quiz_id, t.quiz_sessions.c.user_id == user_id,
            t.quiz_sessions.c.subject_id.is_(None) | (t.subjects.c.user_id == user_id))).mappings().first()
        if row is None:
            raise ApiError("NO_QUIZ", "퀴즈를 찾지 못했어요", 404)
        return row_dict(row)

    def lock_quiz(self, user_id, quiz_id) -> dict:
        user_id = normalized_uuid(user_id)
        try:
            user = lock_user(self.connection, UUID(user_id))
        except UserBusyError:
            raise ApiError("REQUEST_IN_PROGRESS", "이전 요청을 처리 중이에요", 409) from None
        if user is None:
            raise ApiError("NO_QUIZ", "퀴즈를 찾지 못했어요", 404)
        return self.quiz(user_id, quiz_id)

    def items(self, user_id, quiz_id) -> list[dict]:
        quiz = self.quiz(user_id, quiz_id)
        rows = self.connection.execute(select(t.quiz_items, t.questions.c.type, t.questions.c.choices,
            t.questions.c.unit_id, t.units.c.material_id, t.materials.c.subject_id,
            t.subjects.c.user_id.label("question_owner"))
            .join(t.questions, t.quiz_items.c.question_id == t.questions.c.id)
            .join(t.units, t.questions.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
            .where(t.quiz_items.c.quiz_session_id == quiz["id"])
            .order_by(t.quiz_items.c.sort_order, t.quiz_items.c.id)).mappings().all()
        if any(row["question_owner"] != quiz["user_id"] or (
                quiz["subject_id"] is not None and row["subject_id"] != quiz["subject_id"]) for row in rows):
            raise ApiError("NO_QUIZ", "퀴즈 문항을 확인하지 못했어요", 404)
        return [dict(row) for row in rows]

    def item(self, user_id, quiz_id, item_id) -> dict:
        item_id = normalized_uuid(item_id)
        item = next((item for item in self.items(user_id, quiz_id) if item["id"] == item_id), None)
        if item is None:
            raise ApiError("INVALID_INPUT", "현재 퀴즈의 문항이 아니에요", 422)
        return item

    def _current(self, item_id) -> dict | None:
        return row_dict(self.connection.execute(select(t.quiz_answers).join(
            t.quiz_answer_heads, t.quiz_answer_heads.c.answer_id == t.quiz_answers.c.id).where(
            t.quiz_answer_heads.c.quiz_item_id == item_id, t.quiz_answers.c.quiz_item_id == item_id)).mappings().first())

    def current(self, user_id, quiz_id, item_id) -> dict | None:
        item = self.item(user_id, quiz_id, item_id)
        return self._current(item["id"])

    def list_current(self, user_id, quiz_id) -> list[dict]:
        item_ids = [item["id"] for item in self.items(user_id, quiz_id)]
        rows = self.connection.execute(select(t.quiz_answers).join(
            t.quiz_answer_heads, t.quiz_answer_heads.c.answer_id == t.quiz_answers.c.id).where(
            t.quiz_answers.c.quiz_item_id.in_(item_ids),
            t.quiz_answer_heads.c.quiz_item_id == t.quiz_answers.c.quiz_item_id)).mappings().all()
        by_item = {row["quiz_item_id"]: row_dict(row) for row in rows}
        return [by_item[item_id] for item_id in item_ids if item_id in by_item]

    def submission(self, user_id, quiz_id) -> dict | None:
        quiz = self.quiz(user_id, quiz_id)
        return row_dict(self.connection.execute(select(t.quiz_submission_states).where(
            t.quiz_submission_states.c.quiz_session_id == quiz["id"])).mappings().first())

    def seal(self, user_id, quiz_id, *, expected_revisions: dict[str, int], now: datetime) -> dict:
        now = aware_time(now)
        if not isinstance(expected_revisions, dict) or any(type(value) is not int or value < 1
                                                          for value in expected_revisions.values()):
            raise ApiError("INVALID_INPUT", "모든 문항의 답안 revision이 필요해요", 422)
        expected = {normalized_uuid(item_id): value for item_id, value in expected_revisions.items()}
        if len(expected) != len(expected_revisions):
            raise ApiError("INVALID_INPUT", "문항 식별자가 중복되었어요", 422)
        quiz = self.lock_quiz(user_id, quiz_id)
        with self.connection.begin_nested():
            existing = self.submission(user_id, quiz["id"])
            if existing:
                previous = {answer["questionId"]: answer["revision"] for answer in existing["sealed_answers"]}
                if expected != previous:
                    raise ApiError("QUIZ_FINALIZED", "이미 다른 답안 집합으로 봉인한 퀴즈예요", 409)
                return existing
            if quiz["status"] in ("submitted", "carried_over") or quiz["finalized_at"] is not None:
                raise ApiError("QUIZ_FINALIZED", "이미 완료한 퀴즈예요", 409)
            items = self.items(user_id, quiz["id"])
            current = self.list_current(user_id, quiz["id"])
            if not items or set(expected) != {item["id"] for item in items} or len(current) != len(items):
                raise ApiError("INVALID_INPUT", "모든 문항의 답안을 먼저 저장해 주세요", 422)
            if any(answer["revision"] != expected[answer["quiz_item_id"]] or not answer["input_recovered"] for answer in current):
                raise ApiError("ANSWER_CONFLICT", "현재 답안 revision을 다시 확인해 주세요", 409)
            sealed = [{"questionId": answer["quiz_item_id"], "answerId": answer["id"],
                       "revision": answer["revision"], "answerHash": answer["answer_hash"]}
                      for answer in sorted(current, key=lambda answer: answer["quiz_item_id"])]
            digest = canonical_hash([{key: answer[key] for key in ("questionId", "revision", "answerHash")}
                                     for answer in sealed])
            self.connection.execute(t.quiz_submission_states.insert().values(
                quiz_session_id=quiz["id"], sealed_at=now, answer_set_hash=digest,
                input_recovered=True, sealed_answers=sealed, legacy=False))
            return self.submission(user_id, quiz["id"])

    def save(self, user_id, quiz_id, item_id, *, revision: int, choice: int | None = None,
             text: str | None = None, confidence: str, now: datetime, answered_date: date) -> AnswerSave:
        if type(revision) is not int or revision < 0 or type(answered_date) is not date:
            raise ApiError("INVALID_INPUT", "답안 revision 또는 학습 날짜가 올바르지 않아요", 422)
        now = aware_time(now)
        payload = answer_payload(choice=choice, text=text, confidence=confidence)
        digest = canonical_hash(payload)
        quiz = self.lock_quiz(user_id, quiz_id)
        with self.connection.begin_nested():
            item = self.item(user_id, quiz["id"], item_id)
            if item["type"] in ("choice", "ox"):
                if choice is None or choice >= len(item["choices"] or []):
                    raise ApiError("INVALID_INPUT", "유효한 보기 번호가 필요해요", 422)
            elif item["type"] not in ("short", "essay") or text is None:
                raise ApiError("INVALID_INPUT", "문항 유형에 맞는 문자열 답안이 필요해요", 422)
            current = self._current(item["id"])
            current_revision = current["revision"] if current else 0
            if revision < current_revision:
                replay = row_dict(self.connection.execute(select(t.quiz_answers).where(
                    t.quiz_answers.c.quiz_item_id == item["id"], t.quiz_answers.c.revision == revision + 1)).mappings().first())
                if replay and replay["answer_hash"] == digest:
                    return AnswerSave(replay, False, replay["id"] == current["id"])
            elif revision == current_revision and current and current["answer_hash"] == digest:
                return AnswerSave(current, False, True)
            sealed = self.connection.scalar(select(t.quiz_submission_states.c.quiz_session_id).where(
                t.quiz_submission_states.c.quiz_session_id == quiz["id"]))
            if sealed or quiz["status"] in ("submitted", "carried_over") or quiz["finalized_at"] is not None:
                raise ApiError("QUIZ_FINALIZED", "이미 봉인한 퀴즈의 답은 바꿀 수 없어요", 409)
            if revision != current_revision:
                raise ApiError("ANSWER_CONFLICT", "답안이 바뀌었어요. 현재 답안을 다시 확인해 주세요", 409)
            answer = {"id": str(uuid4()), "quiz_item_id": item["id"], "revision": revision + 1, **payload,
                      "answer_hash": digest, "input_recovered": True, "legacy_answer": None,
                      "answered_at": now, "answered_date": answered_date}
            self.connection.execute(t.quiz_answers.insert().values(**answer))
            if current:
                changed = self.connection.execute(update(t.quiz_answer_heads).where(
                    t.quiz_answer_heads.c.quiz_item_id == item["id"], t.quiz_answer_heads.c.answer_id == current["id"]
                ).values(answer_id=answer["id"]))
                if changed.rowcount != 1:
                    raise ApiError("ANSWER_CONFLICT", "답안이 바뀌었어요. 다시 확인해 주세요", 409)
                self.connection.execute(update(t.quiz_grading_jobs).where(
                    t.quiz_grading_jobs.c.answer_id == current["id"],
                    t.quiz_grading_jobs.c.status.in_(("queued", "running", "failed")))
                    .values(status="cancelled", lease_owner=None, lease_token=None, lease_expires_at=None,
                            result=null(), error_code="ANSWER_SUPERSEDED", finished_at=now))
            else:
                self.connection.execute(t.quiz_answer_heads.insert().values(quiz_item_id=item["id"], answer_id=answer["id"]))
            return AnswerSave(answer, True, True)
