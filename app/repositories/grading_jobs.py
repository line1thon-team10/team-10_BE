"""Durable per-answer jobs. External grading runs after the claim is committed."""
from datetime import datetime, timedelta
import re
from uuid import uuid4

from sqlalchemy import Connection, and_, exists, null, or_, select, update

from app.core.responses import ApiError
from app.ai.gateway import InvalidOutput, canonical_json, parse_json
from app.db import schema as t
from app.repositories.quiz_answers import QuizAnswerRepository, aware_time, normalized_uuid, row_dict


def _expiry(now, lease_for):
    if not isinstance(lease_for, timedelta) or lease_for <= timedelta(0):
        raise ApiError("INVALID_INPUT", "lease 기간은 양수여야 해요", 422)
    try:
        return now + lease_for
    except OverflowError:
        raise ApiError("INVALID_INPUT", "lease 만료 시각이 범위를 벗어났어요", 422) from None


def _owner(owner):
    if not isinstance(owner, str) or not owner.strip():
        raise ApiError("INVALID_INPUT", "작업 소유자가 필요해요", 422)
    return owner


def execution_mode(value):
    if value is not None and (type(value) is not str or value not in ("live", "saved", "backup")):
        raise ApiError("INVALID_INPUT", "채점 실행 모드가 올바르지 않아요", 422)
    return value


class ExecutionModeConflict(Exception):
    """Internal outcome. Q-05 decides the HTTP mapping, without an implicit upgrade."""
    def __init__(self, stored_mode, requested_mode):
        self.stored_mode, self.requested_mode = stored_mode, requested_mode
        super().__init__("An explicit retry is required to change a grading execution mode")


class GradingJobRepository:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.answers = QuizAnswerRepository(connection)

    def get(self, user_id, job_id) -> dict:
        row = self.connection.execute(select(t.quiz_grading_jobs, t.quiz_answers.c.quiz_item_id,
            t.quiz_answers.c.revision.label("stored_revision"), t.quiz_answers.c.input_recovered,
            t.quiz_items.c.quiz_session_id, t.quiz_answer_heads.c.answer_id.label("current_answer_id"))
            .join(t.quiz_answers, t.quiz_grading_jobs.c.answer_id == t.quiz_answers.c.id)
            .join(t.quiz_items, t.quiz_answers.c.quiz_item_id == t.quiz_items.c.id)
            .outerjoin(t.quiz_answer_heads, t.quiz_answer_heads.c.quiz_item_id == t.quiz_items.c.id)
            .where(t.quiz_grading_jobs.c.id == normalized_uuid(job_id))).mappings().first()
        if row is None:
            raise ApiError("NO_QUIZ", "채점할 퀴즈를 찾지 못했어요", 404)
        row = row_dict(row)
        item = self.answers.item(user_id, row["quiz_session_id"], row["quiz_item_id"])
        row["question_type"] = item["type"]
        return row

    def _locked(self, user_id, job_id) -> dict:
        job = self.get(user_id, job_id)
        self.answers.lock_quiz(user_id, job["quiz_session_id"])
        return self.get(user_id, job_id)

    @staticmethod
    def _is_current(job):
        return (job["input_recovered"] and job["answer_id"] == job["current_answer_id"]
                and job["answer_revision"] == job["stored_revision"])

    @staticmethod
    def _current_condition(job):
        return exists(select(t.quiz_answer_heads.c.quiz_item_id).where(
            t.quiz_answer_heads.c.quiz_item_id == job["quiz_item_id"],
            t.quiz_answer_heads.c.answer_id == job["answer_id"]))

    def enqueue(self, user_id, quiz_id, item_id, *, revision: int, now: datetime, mode: str | None = None) -> dict:
        mode = execution_mode(mode)
        if type(revision) is not int or revision < 1:
            raise ApiError("INVALID_INPUT", "저장된 답안 revision이 필요해요", 422)
        now = aware_time(now)
        self.answers.lock_quiz(user_id, quiz_id)
        with self.connection.begin_nested():
            answer = self.answers.current(user_id, quiz_id, item_id)
            if not answer or answer["revision"] != revision:
                raise ApiError("ANSWER_CONFLICT", "현재 답안 revision이 아니에요", 409)
            if not answer["input_recovered"]:
                raise ApiError("ANSWER_CONFLICT", "원래 답안을 복원할 수 없는 기록이에요", 409)
            existing = self.connection.scalar(select(t.quiz_grading_jobs.c.id).where(
                t.quiz_grading_jobs.c.answer_id == answer["id"]))
            if existing:
                job = self.get(user_id, existing)
                if mode is not None and job["execution_mode"] != mode:
                    raise ExecutionModeConflict(job["execution_mode"], mode)
                return job
            job_id = str(uuid4())
            self.connection.execute(t.quiz_grading_jobs.insert().values(
                id=job_id, answer_id=answer["id"], answer_revision=revision, status="queued", execution_mode=mode or "live",
                claim_count=0, created_at=now))
            return self.get(user_id, job_id)

    def available(self, user_id, *, now: datetime, limit: int = 100) -> list[dict]:
        now = aware_time(now)
        if type(limit) is not int or limit < 1:
            raise ApiError("INVALID_INPUT", "작업 조회 개수가 올바르지 않아요", 422)
        ids = self.connection.scalars(select(t.quiz_grading_jobs.c.id)
            .join(t.quiz_answers, t.quiz_grading_jobs.c.answer_id == t.quiz_answers.c.id)
            .join(t.quiz_answer_heads, t.quiz_answer_heads.c.answer_id == t.quiz_answers.c.id)
            .join(t.quiz_items, t.quiz_answers.c.quiz_item_id == t.quiz_items.c.id)
            .join(t.quiz_sessions, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .where(t.quiz_sessions.c.user_id == normalized_uuid(user_id), t.quiz_answers.c.input_recovered.is_(True),
                   t.quiz_answer_heads.c.quiz_item_id == t.quiz_answers.c.quiz_item_id,
                   t.quiz_grading_jobs.c.answer_revision == t.quiz_answers.c.revision,
                   or_(t.quiz_grading_jobs.c.status.in_(("queued", "failed")),
                       and_(t.quiz_grading_jobs.c.status == "running", t.quiz_grading_jobs.c.lease_expires_at <= now)))
            .order_by(t.quiz_grading_jobs.c.created_at, t.quiz_grading_jobs.c.id).limit(limit)).all()
        return [self.get(user_id, job_id) for job_id in ids]

    def pending(self, *, now: datetime, limit: int = 100) -> list[dict]:
        """Automatic workers never retry failed jobs, even after a key is added."""
        now = aware_time(now)
        if type(limit) is not int or limit < 1:
            raise ApiError("INVALID_INPUT", "작업 조회 개수가 올바르지 않아요", 422)
        return [dict(row) for row in self.connection.execute(select(
            t.quiz_grading_jobs.c.id, t.quiz_sessions.c.user_id)
            .join(t.quiz_answers, t.quiz_grading_jobs.c.answer_id == t.quiz_answers.c.id)
            .join(t.quiz_answer_heads, t.quiz_answer_heads.c.answer_id == t.quiz_answers.c.id)
            .join(t.quiz_items, t.quiz_answers.c.quiz_item_id == t.quiz_items.c.id)
            .join(t.quiz_sessions, t.quiz_items.c.quiz_session_id == t.quiz_sessions.c.id)
            .join(t.questions, t.quiz_items.c.question_id == t.questions.c.id)
            .join(t.units, t.questions.c.unit_id == t.units.c.id)
            .join(t.materials, t.units.c.material_id == t.materials.c.id)
            .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
            .where(t.quiz_answer_heads.c.quiz_item_id == t.quiz_answers.c.quiz_item_id,
                t.quiz_answers.c.input_recovered.is_(True), t.quiz_grading_jobs.c.answer_revision == t.quiz_answers.c.revision,
                t.subjects.c.user_id == t.quiz_sessions.c.user_id,
                or_(t.quiz_sessions.c.subject_id.is_(None), t.quiz_sessions.c.subject_id == t.subjects.c.id),
                or_(t.quiz_grading_jobs.c.status == "queued",
                    and_(t.quiz_grading_jobs.c.status == "running", t.quiz_grading_jobs.c.lease_expires_at <= now)))
            .order_by(t.quiz_grading_jobs.c.created_at, t.quiz_grading_jobs.c.id).limit(limit)).mappings()]

    def claim(self, user_id, job_id, *, owner: str, now: datetime, lease_for: timedelta,
              retry_failed: bool = True, mode: str | None = None) -> dict | None:
        if type(retry_failed) is not bool:
            raise ApiError("INVALID_INPUT", "재시도 여부가 올바르지 않아요", 422)
        mode = execution_mode(mode)
        now, owner = aware_time(now), _owner(owner)
        expires = _expiry(now, lease_for)
        job = self._locked(user_id, job_id)
        if not self._is_current(job):
            return None
        if job["status"] in ("succeeded", "cancelled") or (job["status"] == "running" and job["lease_expires_at"] > now):
            return None
        if job["status"] == "failed" and not retry_failed:
            return None
        if mode is not None and mode != job["execution_mode"] and not retry_failed:
            raise ExecutionModeConflict(job["execution_mode"], mode)
        eligible = or_(t.quiz_grading_jobs.c.status.in_(("queued", "failed") if retry_failed else ("queued",)),
                       and_(t.quiz_grading_jobs.c.status == "running", t.quiz_grading_jobs.c.lease_expires_at <= now))
        changed = self.connection.execute(update(t.quiz_grading_jobs).where(
            t.quiz_grading_jobs.c.id == job["id"], t.quiz_grading_jobs.c.answer_revision == job["stored_revision"],
            t.quiz_grading_jobs.c.status == job["status"], t.quiz_grading_jobs.c.lease_token == job["lease_token"],
            t.quiz_grading_jobs.c.execution_mode == job["execution_mode"],
            self._current_condition(job), eligible).values(
            status="running", lease_owner=owner, lease_token=str(uuid4()), lease_expires_at=expires,
            execution_mode=mode or job["execution_mode"],
            claim_count=t.quiz_grading_jobs.c.claim_count + 1, claimed_at=now, finished_at=None,
            result=null(), error_code=None))
        return self.get(user_id, job_id) if changed.rowcount == 1 else None

    def _lease_conditions(self, job, owner, token, revision, now):
        return (t.quiz_grading_jobs.c.id == job["id"], t.quiz_grading_jobs.c.status == "running",
                t.quiz_grading_jobs.c.lease_owner == _owner(owner),
                t.quiz_grading_jobs.c.lease_token == normalized_uuid(token),
                t.quiz_grading_jobs.c.answer_revision == revision,
                t.quiz_grading_jobs.c.claimed_at <= now, t.quiz_grading_jobs.c.lease_expires_at > now,
                self._current_condition(job))

    def renew(self, user_id, job_id, *, owner: str, token, revision: int,
              now: datetime, lease_for: timedelta) -> bool:
        now = aware_time(now)
        expires = _expiry(now, lease_for)
        job = self._locked(user_id, job_id)
        if type(revision) is not int or not self._is_current(job) or revision != job["stored_revision"]:
            return False
        expires = max(expires, job["lease_expires_at"] or expires)
        changed = self.connection.execute(update(t.quiz_grading_jobs).where(
            *self._lease_conditions(job, owner, token, revision, now)).values(lease_expires_at=expires))
        return changed.rowcount == 1

    @staticmethod
    def _validated_result(result: dict, question_type: str) -> dict:
        keys = {"is_correct", "essay_score", "feedback", "got_points", "missing_points", "grading_status", "provenance"}
        if not isinstance(result, dict) or set(result) - keys or type(result.get("is_correct")) is not bool:
            raise ApiError("INVALID_INPUT", "검증된 채점 결과가 필요해요", 422)
        try:
            result = parse_json(canonical_json(result))
        except InvalidOutput:
            raise ApiError("INVALID_INPUT", "저장할 수 없는 채점 결과예요", 422) from None
        score = result.get("essay_score")
        if question_type == "essay":
            if type(score) is not int or not 0 <= score <= 100 or result["is_correct"] != (score >= 85):
                raise ApiError("INVALID_INPUT", "서술형 점수와 정오 판정이 맞지 않아요", 422)
        elif score is not None:
            raise ApiError("INVALID_INPUT", "서술형 외 문항에는 점수를 넣을 수 없어요", 422)
        for field in ("got_points", "missing_points"):
            value = result.get(field)
            if value is not None and (not isinstance(value, list) or any(not isinstance(x, str) for x in value)):
                raise ApiError("INVALID_INPUT", "채점 근거는 문자열 배열이어야 해요", 422)
        if result.get("feedback") is not None and not isinstance(result["feedback"], str):
            raise ApiError("INVALID_INPUT", "채점 피드백은 문자열이어야 해요", 422)
        status = result.get("grading_status", "graded")
        if status not in ("graded", "fallback"):
            raise ApiError("INVALID_INPUT", "완료된 채점 결과가 필요해요", 422)
        provenance = result.get("provenance")
        if provenance is not None:
            hashes = {"answer_hash", "question_hash", "source_hash", "material_hash"}
            versions = {"model_version", "prompt_version", "schema_version"}
            if type(provenance) is not dict or set(provenance) != hashes | versions | {"saved_result_id"}:
                raise ApiError("INVALID_INPUT", "채점 provenance 필드가 올바르지 않아요", 422)
            if any(type(provenance[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", provenance[key]) is None for key in hashes):
                raise ApiError("INVALID_INPUT", "채점 입력 해시가 올바르지 않아요", 422)
            if any(type(provenance[key]) is not str or not provenance[key].strip() for key in versions):
                raise ApiError("INVALID_INPUT", "채점 버전이 필요해요", 422)
            if provenance["saved_result_id"] is not None:
                provenance["saved_result_id"] = normalized_uuid(provenance["saved_result_id"])
        return {key: result.get(key) for key in keys - {"grading_status"}} | {"grading_status": status}

    def _check_provenance(self, user_id, job, provenance):
        if provenance is None:
            return  # Legacy results have no inferred version evidence.
        answer_hash = self.connection.scalar(select(t.quiz_answers.c.answer_hash).where(t.quiz_answers.c.id == job["answer_id"]))
        if provenance["answer_hash"] != answer_hash:
            raise ApiError("INVALID_INPUT", "채점 provenance가 현재 답안과 다릅니다", 422)
        saved_id = provenance["saved_result_id"]
        if saved_id is not None and self.connection.scalar(select(t.ai_saved_results.c.id).where(
            t.ai_saved_results.c.id == saved_id, t.ai_saved_results.c.user_id == normalized_uuid(user_id),
            t.ai_saved_results.c.kind == "essay_grade", t.ai_saved_results.c.schema_version == provenance["schema_version"],
        )) is None:
            raise ApiError("INVALID_INPUT", "채점 저장 결과의 소유자 또는 버전이 다릅니다", 422)

    def complete(self, user_id, job_id, *, owner: str, token, revision: int, now: datetime, result: dict) -> bool:
        now = aware_time(now)
        job = self._locked(user_id, job_id)
        if type(revision) is not int or not self._is_current(job) or revision != job["stored_revision"]:
            return False
        result = self._validated_result(result, job["question_type"])
        self._check_provenance(user_id, job, result["provenance"])
        changed = self.connection.execute(update(t.quiz_grading_jobs).where(
            *self._lease_conditions(job, owner, token, revision, now)).values(
            status="succeeded", result=result, error_code=None, finished_at=now,
            lease_owner=None, lease_token=None, lease_expires_at=None))
        return changed.rowcount == 1

    def fail(self, user_id, job_id, *, owner: str, token, revision: int, now: datetime, error_code: str) -> bool:
        now = aware_time(now)
        if not isinstance(error_code, str) or not error_code.strip():
            raise ApiError("INVALID_INPUT", "실패 코드가 필요해요", 422)
        job = self._locked(user_id, job_id)
        if type(revision) is not int or not self._is_current(job) or revision != job["stored_revision"]:
            return False
        changed = self.connection.execute(update(t.quiz_grading_jobs).where(
            *self._lease_conditions(job, owner, token, revision, now)).values(
            status="failed", result=null(), error_code=error_code, finished_at=now,
            lease_owner=None, lease_token=None, lease_expires_at=None))
        return changed.rowcount == 1
