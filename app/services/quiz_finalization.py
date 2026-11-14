"""Commit sealed inputs before grading; publish a complete result exactly once."""
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select, update
from fastapi.encoders import jsonable_encoder

from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import GradingJobRepository
from app.repositories.quiz_answers import QuizAnswerRepository, answer_payload, canonical_hash
from app.schemas.quiz import SubmitIn
from app.services.grading import GradingBudget, GradingService, validated_completed_result
from app.services.quiz import QuizService
from app.services.quiz_answers import lock_quiz_user
from app.services.study import OPEN_RATE


def _pending():
    return ApiError("AI_FAILED", "채점이 아직 끝나지 않았어요. 같은 답안으로 다시 제출해 주세요", 503)


def legacy_result(connection, user_id, quiz_id, *, seen=frozenset()):
    """Freeze the first reconstruction from preserved facts, not a past response."""
    user = lock_quiz_user(connection, {"id": user_id})
    now = now_utc()
    repo = QuizAnswerRepository(connection)
    quiz = repo.quiz(user_id, quiz_id)
    items = repo.items(user_id, quiz_id)
    state = repo.submission(user_id, quiz_id)
    if state is not None and state["result_snapshot"] is not None:
        return deepcopy(state["result_snapshot"])
    if (quiz["status"] != "submitted" or quiz["grading_status"] != "completed"
            or quiz["finalized_at"] is None or (state is not None and not state["legacy"])):
        raise _pending()
    attempts = [dict(row) for row in connection.execute(select(t.quiz_attempts).where(
        t.quiz_attempts.c.quiz_item_id.in_([item["id"] for item in items]))).mappings()]
    if (not items or len(attempts) != len(items) or any(attempt["is_correct"] is None
            or attempt["grading_status"] not in ("graded", "fallback") or attempt["finalized_at"] is None
            or attempt["graded_at"] is None or attempt["confidence"] not in ("sure", "unsure")
            for attempt in attempts)):
        raise _pending()
    if state is None:
        # Old submit/seed writers can create finalized facts after migration0006.
        # Preserve their times/scores; do not infer a choice index or input hash.
        connection.execute(t.quiz_submission_states.insert().values(
            quiz_session_id=quiz_id, sealed_at=quiz["finalized_at"], answer_set_hash=None,
            input_recovered=False, sealed_answers=[], legacy=True,
            legacy_snapshot=jsonable_encoder({"schemaVersion": "legacy-current-facts-v1", "session": quiz, "attempts": attempts})))
        state = repo.submission(user_id, quiz_id)
    today = user.get("demo_date") or now.astimezone(ZoneInfo(user.get("timezone") or "Asia/Seoul")).date()
    result = QuizService(connection, user, today)._build_result_view(quiz, seen=seen)
    stored = repo.store_result_snapshot(user_id, quiz_id, expected_hash=state["answer_set_hash"], snapshot=result,
        provenance="legacy_reconstructed", schema_version="quiz-result-legacy-v1", now=now)
    return deepcopy(stored["result_snapshot"])


@dataclass(frozen=True)
class SealedSubmission:
    quiz_id: str
    answer_set_hash: str
    job_ids: tuple[str, ...]
    busy: bool = False
    result: dict | None = None


class QuizFinalizationService:
    def __init__(self, engine: Engine, *, clock=now_utc, worker_factory=None):
        if not isinstance(engine, Engine):
            raise TypeError("Finalization owns short Engine transactions")
        self.engine, self.clock = engine, clock
        self.worker = (worker_factory or GradingService)(engine, owner="submit-" + str(uuid4()), clock=clock)

    def _context(self, conn, user_id):
        user = lock_quiz_user(conn, {"id": user_id})
        now = self.clock()
        today = user.get("demo_date") or now.astimezone(ZoneInfo(user.get("timezone") or "Asia/Seoul")).date()
        return user, now, QuizService(conn, user, today)

    def _admit(self, conn, user_id, key, body: SubmitIn):
        user, now, view = self._context(conn, user_id)
        quiz = view.session(key, quiz_id=str(body.quizId))
        with QuizAnswerRepository(conn).checked_items(user_id, quiz["id"]):
            return self._admit_locked(conn, user_id, quiz, body, now, view)

    def _admit_locked(self, conn, user_id, quiz, body, now, view):
        answers, jobs = QuizAnswerRepository(conn), GradingJobRepository(conn)
        items = answers.items(user_id, quiz["id"])
        payloads = {str(answer.questionId): answer_payload(choice=answer.choice, text=answer.text,
                    confidence=answer.confidence) for answer in body.answers}
        if len(payloads) != len(body.answers) or set(payloads) != {item["id"] for item in items} or not items:
            raise ApiError("INVALID_INPUT", "모든 퀴즈 문항을 정확히 한 번 제출해 주세요", 422)
        state = answers.submission(user_id, quiz["id"])
        if state is not None:
            expected = {item["questionId"]: item["answerHash"] for item in state["sealed_answers"]}
            if not state["input_recovered"] or expected != {key: canonical_hash(value) for key, value in payloads.items()}:
                raise ApiError("QUIZ_FINALIZED", "이미 제출한 답안은 바꿀 수 없어요", 409)
            if state["result_snapshot"] is not None:
                return SealedSubmission(quiz["id"], state["answer_set_hash"], (), result=deepcopy(state["result_snapshot"]))
            if state["legacy"]:
                raise ApiError("QUIZ_FINALIZED", "과거 제출은 결과 조회에서 확인해 주세요", 409)
        else:
            if quiz["status"] in ("submitted", "carried_over") or quiz["finalized_at"] is not None:
                raise ApiError("QUIZ_FINALIZED", "이미 완료한 퀴즈예요. 결과를 조회해 주세요", 409)
            if quiz["status"] not in ("ready", "in_progress"):
                raise ApiError("NO_QUIZ", "아직 풀 수 있는 퀴즈가 아니에요", 404)
            if quiz["type"] == "daily" and not view.data.quiz_progress(view.today)["subjectOpen"].get(quiz["subject_id"], False):
                raise ApiError("QUIZ_LOCKED", f"오늘 계획의 {OPEN_RATE}%를 넘기면 열려요", 403)
            view._validated_answers(view.items(quiz["id"]), [answer.model_dump(mode="json") for answer in body.answers])
            revisions = {}
            for item in items:
                current = answers.current(user_id, quiz["id"], item["id"])
                saved = answers.save(user_id, quiz["id"], item["id"], revision=current["revision"] if current else 0,
                    now=now, answered_date=view.today, **payloads[item["id"]])
                revisions[item["id"]] = saved.answer["revision"]
                jobs.enqueue(user_id, quiz["id"], item["id"], revision=saved.answer["revision"], now=now,
                             mode="saved" if body.savedGrading and saved.created else None)
            state = answers.seal(user_id, quiz["id"], expected_revisions=revisions, now=now)
        current = self._sealed_answers(answers, user_id, quiz["id"], state)
        pending, busy = [], False
        for item in state["sealed_answers"]:
            job_id = conn.scalar(select(t.quiz_grading_jobs.c.id).where(
                t.quiz_grading_jobs.c.answer_id == item["answerId"]))
            if job_id is None:
                raise _pending()
            if validated_completed_result(conn, user_id, job_id) is not None:
                continue
            job = jobs.get(user_id, job_id)
            ready = jobs.submission_retry(user_id, job_id, revision=current[item["questionId"]]["revision"],
                now=now, mode="saved" if body.savedGrading else None, stale_succeeded=job["status"] == "succeeded")
            busy = busy or not ready
            if ready:
                pending.append(job_id)
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz["id"]).values(
            status="in_progress", grading_status="processing", submitted_at=quiz["submitted_at"] or now))
        return SealedSubmission(quiz["id"], state["answer_set_hash"], tuple(pending), busy=busy)

    @staticmethod
    def _sealed_answers(repo, user_id, quiz_id, state):
        current = {answer["quiz_item_id"]: answer for answer in repo.list_current(user_id, quiz_id)}
        sealed = {item["questionId"]: item for item in state["sealed_answers"]}
        if set(current) != set(sealed) or any(
                answer["id"] != sealed[item_id]["answerId"] or answer["revision"] != sealed[item_id]["revision"]
                or answer["answer_hash"] != sealed[item_id]["answerHash"] or not answer["input_recovered"]
                or answer["answer_hash"] != canonical_hash({key: answer[key] for key in ("choice", "text", "confidence")})
                for item_id, answer in current.items()):
            raise ApiError("ANSWER_CONFLICT", "봉인한 답안 집합이 바뀌었어요", 409)
        return current

    def submit(self, user_id: str, key: str, body: SubmitIn) -> dict:
        budget = GradingBudget.start(self.worker.settings.request_budget_seconds)
        with self.engine.begin() as conn:
            admitted = self._admit(conn, user_id, key, body)
        if admitted.result is not None:
            return admitted.result
        if admitted.busy:
            raise _pending()
        # No caller connection or user lock survives across these executions.
        for job_id in admitted.job_ids:
            if not budget.remaining:
                break
            try:
                self.worker.run_job(user_id, job_id, retry_failed=True, budget=budget)
            except ApiError as exc:
                if exc.code not in ("REQUEST_IN_PROGRESS", "AI_FAILED"):
                    raise
        with self.engine.begin() as conn:
            result = self._finalize(conn, user_id, admitted)
        if result is None:
            raise _pending()
        return result

    def _finalize(self, conn, user_id, admitted):
        user, now, view = self._context(conn, user_id)
        with QuizAnswerRepository(conn).checked_items(user_id, admitted.quiz_id):
            return self._finalize_locked(conn, user_id, admitted, now, view)

    def _finalize_locked(self, conn, user_id, admitted, now, view):
        answers, jobs = QuizAnswerRepository(conn), GradingJobRepository(conn)
        quiz = answers.quiz(user_id, admitted.quiz_id)
        state = answers.submission(user_id, quiz["id"])
        if state is None or state["answer_set_hash"] != admitted.answer_set_hash:
            raise ApiError("ANSWER_CONFLICT", "봉인한 답안 집합이 바뀌었어요", 409)
        if state["result_snapshot"] is not None:
            return deepcopy(state["result_snapshot"])
        current = self._sealed_answers(answers, user_id, quiz["id"], state)
        results, statuses = {}, []
        for item_id, answer in current.items():
            job_id = conn.scalar(select(t.quiz_grading_jobs.c.id).where(t.quiz_grading_jobs.c.answer_id == answer["id"]))
            if job_id is None:
                raise _pending()
            statuses.append(jobs.get(user_id, job_id)["status"])
            results[item_id] = validated_completed_result(conn, user_id, job_id)
        if not results or any(result is None for result in results.values()):
            conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz["id"]).values(
                grading_status="processing" if any(status in ("queued", "running") for status in statuses) else "failed"))
            return None
        carried = quiz['status']=='carried_over' and quiz['submitted_at'] is None
        items = view.items(quiz["id"])
        if carried:
            items = [item for item in items if item['item_id'] in current]
        if any(item["attempt_id"] is not None for item in items):
            raise _pending()  # never overwrite unexplained legacy/partial finalized facts
        before_blocks = {uid: [view.data.block_state(task, view.today) for task in view.data.tasks
            if view.data.unit_of(task) and view.data.unit_of(task)["id"] == uid
            and task.row["status"] not in ("carried_over", "excluded")]
            for uid in {item["unit_id"] for item in items}}
        for item in items:
            answer, result = current[item["item_id"]], results[item["item_id"]]
            original = item["choices"][answer["choice"]]["text"] if answer["choice"] is not None else answer["text"]
            conn.execute(t.quiz_attempts.insert().values(id=str(uuid4()), quiz_item_id=item["item_id"],
                answer=original, confidence=answer["confidence"], is_correct=result["is_correct"],
                grading_status=result["grading_status"], essay_score=result["essay_score"], feedback=result["feedback"],
                got_points=result["got_points"], missing_points=result["missing_points"],
                answered_at=answer["answered_at"], answered_date=answer["answered_date"],
                finalized_at=now, graded_at=now, mastery_applied_at=now))
            view._update_weak(item["question_id"], result["is_correct"] and answer["confidence"] == "sure", now)
        conn.execute(update(t.quiz_sessions).where(t.quiz_sessions.c.id == quiz["id"]).values(
            status="carried_over" if carried else "submitted", grading_status="completed", current_position=len(items),
            finalized_at=quiz["finalized_at"] or now, graded_at=now,
            is_fallback=any(result["grading_status"] == "fallback" for result in results.values())))
        view._recalc_mastery(quiz["id"], {item["unit_id"] for item in items})
        result = view._build_result_view(answers.quiz(user_id, quiz["id"]), before_blocks=before_blocks,
            item_ids=set(current) if carried else None)
        stored = answers.store_result_snapshot(user_id, quiz["id"], expected_hash=state["answer_set_hash"],
            snapshot=result, provenance="submitted", schema_version="quiz-result-v1", now=now)
        if (stored["result_snapshot"] != result or stored["result_provenance"] != "submitted"
                or stored["result_captured_at"] != now):
            # A lost first-snapshot race must also undo this transaction's
            # attempts/mastery/weak writes, not return a competing response.
            raise ApiError("ANSWER_CONFLICT", "결과 확정 상태가 바뀌었어요", 409)
        return deepcopy(stored["result_snapshot"])


def finish_carried(connection, user, quiz_id, *, at=None):
    """Publish already graded saved answers; this function never invokes a provider."""
    now = at or now_utc()
    user = lock_quiz_user(connection, user)
    answers = QuizAnswerRepository(connection)
    quiz = answers.quiz(user['id'], quiz_id)
    state = answers.submission(user['id'], quiz_id)
    if quiz['status']!='carried_over' or state is None or state['legacy']:
        return None
    if state['result_snapshot'] is not None:
        return deepcopy(state['result_snapshot'])
    today = user.get('demo_date') or now.astimezone(ZoneInfo(user['timezone'])).date()
    view = QuizService(connection,user,today)
    service = QuizFinalizationService(connection.engine,clock=lambda:now)
    with answers.checked_items(user['id'],quiz_id):
        return service._finalize_locked(connection,user['id'],
            SealedSubmission(quiz_id,state['answer_set_hash'],()),now,view)


def close_daily_quizzes(connection, user, day, *, at=None):
    """Atomic date-close hook: freeze answered heads and preserve all original items."""
    now=at or now_utc()
    user=lock_quiz_user(connection,user)
    ids=list(connection.scalars(select(t.quiz_sessions.c.id).where(t.quiz_sessions.c.user_id==user['id'],
        t.quiz_sessions.c.type=='daily',t.quiz_sessions.c.quiz_date<=day,
        t.quiz_sessions.c.status.in_(('ready','in_progress','carried_over')))))
    for identifier in ids:
        state=QuizAnswerRepository(connection).seal_carried(user['id'],identifier,now=now)
        if state is not None:
            try:
                finish_carried(connection,user,identifier,at=now)
            except ApiError as error:
                if error.code != 'AI_FAILED':
                    raise
    return ids
