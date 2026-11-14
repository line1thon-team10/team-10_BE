"""Restartable per-answer grading. Q-05 alone finalizes attempts and mastery."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import math
import time

from sqlalchemy import Engine, select

from app.ai.gateway import (AiGateway, CleanupFailed, GatewayRequest, InvalidOutput, MaterialRevision,
                            ResultIdentity, canonical_json, parse_json)
from app.ai.grading_adapter import MODEL_VERSION, PROMPT_VERSION, PROVIDER, SCHEMA_VERSION, VALIDATOR, validate_evidence
from app.ai.settings import AiSettings
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.grading_jobs import ExecutionModeConflict, GradingJobRepository, execution_mode
from app.repositories.quiz_answers import canonical_hash, normalized_uuid


RULE_VERSION = "deterministic-grade-v1"


@dataclass(frozen=True, slots=True)
class GradingBudget:
    """One server-created absolute deadline shared by every item of a submit."""
    deadline: float

    def __post_init__(self):
        if type(self.deadline) not in (float, int) or not math.isfinite(self.deadline):
            raise ValueError("A finite monotonic deadline is required")

    @classmethod
    def start(cls, seconds=20):
        settings = AiSettings(request_budget_seconds=seconds)
        return cls(time.monotonic() + settings.request_budget_seconds)

    @property
    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())


class _BoundExecutor:
    def __init__(self, executor, budget):
        self.executor, self.budget = executor, budget

    def execute(self, *args, deadline, **kwargs):
        return self.executor.execute(*args, deadline=min(deadline, self.budget.deadline), **kwargs)


@dataclass(frozen=True, slots=True, repr=False)
class GradingSnapshot:
    user_id: str
    job_id: str
    token: str
    owner: str
    revision: int
    mode: str
    payload_json: str

    @property
    def data(self):
        # Decode a fresh value; callers cannot mutate the captured snapshot.
        return parse_json(self.payload_json)

    @property
    def hashes(self):
        data = self.data
        return {"answer_hash": data["answer"]["answer_hash"], "question_hash": _hash(data["question"]),
                "source_hash": _hash(data["sources"]), "material_hash": _hash(data["material_context"])}


class GradingState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    NOT_CLAIMED = "not_claimed"
    MODE_CONFLICT = "mode_conflict"
    BUDGET_EXHAUSTED = "budget_exhausted"
    IDLE = "idle"


@dataclass(frozen=True, slots=True, repr=False)
class GradingRun:
    job_id: str | None
    state: GradingState
    result: dict | None = None
    error_code: str | None = None
    stored_mode: str | None = None
    requested_mode: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "state", GradingState(self.state))


@dataclass(frozen=True, slots=True, repr=False)
class PreparedGrading:
    snapshot: GradingSnapshot
    request: GatewayRequest | None
    deterministic_json: str | None
    budget: GradingBudget


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_row(row):
    return {str(key): ((value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
                  if isinstance(value, datetime) else value.isoformat() if isinstance(value, date) else value)
            for key, value in dict(row).items()}


def capture_snapshot(connection, user_id, job):
    """Call under user/quiz lock, in the same transaction as claim or recheck."""
    repo = GradingJobRepository(connection)
    item = repo.answers.item(user_id, job["quiz_session_id"], job["quiz_item_id"])
    answer = repo.answers.current(user_id, job["quiz_session_id"], job["quiz_item_id"])
    if not answer or answer["id"] != job["answer_id"] or answer["revision"] != job["answer_revision"]:
        raise InvalidOutput("The current answer no longer matches the grading job")
    if not answer["input_recovered"] or answer["answer_hash"] != canonical_hash({
        "choice": answer["choice"], "text": answer["text"], "confidence": answer["confidence"],
    }):
        raise InvalidOutput("The stored answer hash does not describe its original input")
    question = connection.execute(select(t.questions).where(t.questions.c.id == item["question_id"])).mappings().one()
    unit = connection.execute(select(t.units).where(t.units.c.id == question["unit_id"])).mappings().one()
    sources = [dict(row) for row in connection.execute(select(t.question_sources)
        .where(t.question_sources.c.question_id == question["id"]).order_by(t.question_sources.c.id)).mappings()]
    material_ids = {unit["material_id"], *(source["material_id"] for source in sources)}
    materials = [dict(row) for row in connection.execute(select(t.materials)
        .join(t.subjects, t.materials.c.subject_id == t.subjects.c.id)
        .where(t.materials.c.id.in_(material_ids), t.subjects.c.user_id == normalized_uuid(user_id))
        .order_by(t.materials.c.id)).mappings()]
    if {row["id"] for row in materials} != material_ids:
        raise InvalidOutput("Every question source must be owned by this user")
    unit_fields = ("id", "material_id", "quantity_basis", "start_page", "end_page", "in_scope", "retired_at")
    concept = None
    if question["concept_id"] is not None:
        raw = connection.execute(select(t.concepts).where(t.concepts.c.id == question["concept_id"],
            t.concepts.c.unit_id == question["unit_id"])).mappings().first()
        if raw is None:
            raise InvalidOutput("The concept must belong to this question's unit")
        concept = {key: raw[key] for key in ("id", "unit_id", "start_page", "end_page", "in_scope", "retired_at")}
    data = {"quiz_id": job["quiz_session_id"], "item_id": job["quiz_item_id"], "answer": _json_row(answer),
            "question": _json_row(question), "sources": [_json_row(row) for row in sources],
            "material_context": {"materials": [_json_row(row) for row in materials],
                "unit": _json_row({key: unit[key] for key in unit_fields}),
                "concept": _json_row(concept) if concept else None}}
    if question["type"] not in ("choice", "ox", "short", "essay") or not question["correct_answer"].strip():
        raise InvalidOutput("A supported question with a correct answer is required")
    return GradingSnapshot(normalized_uuid(user_id), job["id"], job["lease_token"], job["lease_owner"],
                           job["answer_revision"], job["execution_mode"], canonical_json(data))


def _deterministic(data):
    question, answer = data["question"], data["answer"]
    score, feedback, good, missing = None, None, None, None
    if question["type"] in ("choice", "ox"):
        choices = question["choices"]
        if type(choices) is not list or any(type(option) is not dict or type(option.get("text")) is not str for option in choices):
            raise InvalidOutput("Stored choices must have textual options")
        indexes = [index for index, option in enumerate(choices) if option["text"] == question["correct_answer"]]
        if len(indexes) != 1 or type(answer["choice"]) is not int or not 0 <= answer["choice"] < len(choices) or answer["text"] is not None:
            raise InvalidOutput("A unique server answer index and valid choice are required")
        correct = answer["choice"] == indexes[0]
    else:
        text = answer["text"]
        if type(text) is not str or answer["choice"] is not None:
            raise InvalidOutput("A text answer is required")
        if question["type"] == "short":
            normalize = lambda value: " ".join(value.split()).lower()
            correct = bool(text.strip()) and normalize(text) == normalize(question["correct_answer"])
        elif text.strip():
            return None
        else:
            correct, score = False, 0
            feedback, good, missing = "답안을 입력하지 않았어요.", [], ["답안 미제출"]
    return {"is_correct": correct, "essay_score": score, "feedback": feedback,
            "got_points": good, "missing_points": missing, "grading_status": "graded"}


def _versions(deterministic):
    return {"model_version": RULE_VERSION if deterministic else MODEL_VERSION,
            "prompt_version": "none" if deterministic else PROMPT_VERSION,
            "schema_version": RULE_VERSION if deterministic else SCHEMA_VERSION}


def essay_request(snapshot):
    data = snapshot.data
    question, sources = data["question"], data["sources"]
    materials = data["material_context"]["materials"]
    evidence = {"question_id": question["id"], "sources": sources, "materials": materials}
    validate_evidence(evidence)
    quoted = [source for source in sources if source["quote"] and source["quote"].strip()]
    text = "\n\n".join(f"[{source['material_id']} p.{source['start_page']}-{source['end_page']}]\n{source['quote']}" for source in quoted)
    payload = {"question": {"id": question["id"], "question": question["prompt"], "answer": question["correct_answer"],
                "source_page": quoted[0]["start_page"], "source_text": text}, "answer": data["answer"]["text"],
               "fingerprints": snapshot.hashes}
    identity = ResultIdentity.create(kind="essay_grade", provider="groq", **_versions(False), input=payload,
        validation_context=evidence, materials=tuple(MaterialRevision(row["id"], _hash(row)) for row in materials))
    # quiz_grading_jobs and ai_jobs have unrelated UUID namespaces/FKs.
    return GatewayRequest(snapshot.user_id, identity, job_id=None)


def validated_completed_result(connection, user_id, job_id):
    """Q-05 calls this inside its finalization transaction; unknown provenance is not current."""
    repo = GradingJobRepository(connection)
    job = repo._locked(user_id, job_id)
    if job["status"] != "succeeded" or job["result"] is None:
        return None
    try:
        snapshot = capture_snapshot(connection, user_id, job)
        result = repo._validated_result(job["result"], job["question_type"])
        provenance = result["provenance"]
        expected = snapshot.hashes | _versions(_deterministic(snapshot.data) is not None)
        if provenance is None or any(provenance[key] != value for key, value in expected.items()):
            return None
        repo._check_provenance(user_id, job, provenance)
        return result
    except (InvalidOutput, ApiError):
        return None


class GradingService:
    def __init__(self, engine, *, owner, settings=None, gateway_factory=AiGateway, clock=now_utc, provider=PROVIDER):
        if not isinstance(engine, Engine):
            raise TypeError("GradingService owns short Engine connections, never a caller Connection")
        self.engine, self.owner, self.clock = engine, owner, clock
        self.settings = settings or AiSettings.from_environment()
        self.gateway_factory, self.provider = gateway_factory, provider

    def prepare(self, connection, user_id, job_id, *, retry_failed=False, mode=None, budget=None):
        """Caller commits this transaction before execute(); mode is durable even after a crash."""
        execution_mode(mode)
        if type(retry_failed) is not bool:
            raise ValueError("Use an explicit boolean retry policy")
        budget = budget or GradingBudget.start(self.settings.request_budget_seconds)
        if not isinstance(budget, GradingBudget):
            raise TypeError("Pass the same GradingBudget for every item of a submit")
        if not budget.remaining:
            return GradingRun(str(job_id), "budget_exhausted", error_code="AI_FAILED")
        repo = GradingJobRepository(connection)
        try:
            lease = repo.claim(user_id, job_id, owner=self.owner, now=self.clock(),
                lease_for=timedelta(seconds=self.settings.request_budget_seconds + 10), retry_failed=retry_failed, mode=mode)
        except ExecutionModeConflict as conflict:
            return GradingRun(str(job_id), "mode_conflict", stored_mode=conflict.stored_mode, requested_mode=conflict.requested_mode)
        if lease is None:
            return GradingRun(str(job_id), "not_claimed")
        try:
            snapshot = capture_snapshot(connection, user_id, lease)
            deterministic = _deterministic(snapshot.data)
            request = None if deterministic is not None else essay_request(snapshot)
        except (InvalidOutput, ValueError, TypeError):
            repo.fail(user_id, job_id, owner=self.owner, token=lease["lease_token"], revision=lease["answer_revision"],
                      now=self.clock(), error_code="INVALID_GRADING_INPUT")
            return GradingRun(str(job_id), "failed", error_code="INVALID_GRADING_INPUT")
        return PreparedGrading(snapshot, request, canonical_json(deterministic) if deterministic is not None else None, budget)

    def run_job(self, user_id, job_id, *, retry_failed=False, mode=None, budget=None):
        with self.engine.begin() as conn:
            prepared = self.prepare(conn, user_id, job_id, retry_failed=retry_failed, mode=mode, budget=budget)
        return prepared if isinstance(prepared, GradingRun) else self.execute(prepared)

    def _recheck(self, connection, snapshot):
        repo = GradingJobRepository(connection)
        try:
            job = repo._locked(snapshot.user_id, snapshot.job_id)
        except ApiError as exc:
            if exc.status == 404:
                return GradingRun(snapshot.job_id, "stale")
            raise
        if (job["status"] != "running" or job["lease_token"] != snapshot.token or job["lease_owner"] != snapshot.owner
                or job["answer_revision"] != snapshot.revision or job["execution_mode"] != snapshot.mode):
            return GradingRun(snapshot.job_id, "stale")
        try:
            current = capture_snapshot(connection, snapshot.user_id, job)
            changed = current.payload_json != snapshot.payload_json
        except (InvalidOutput, ValueError, TypeError):
            changed = True
        now = self.clock()
        if job["lease_expires_at"] <= now:
            return GradingRun(snapshot.job_id, "stale")
        if changed:
            failed = repo.fail(snapshot.user_id, snapshot.job_id, owner=snapshot.owner, token=snapshot.token,
                revision=snapshot.revision, now=now, error_code="GRADING_INPUT_CHANGED")
            return GradingRun(snapshot.job_id, "failed" if failed else "stale", error_code="GRADING_INPUT_CHANGED")
        return None

    def execute(self, prepared):
        if not isinstance(prepared, PreparedGrading):
            raise TypeError("A server-created prepared grading snapshot is required")
        snapshot, budget = prepared.snapshot, prepared.budget
        user_id, job_id = snapshot.user_id, snapshot.job_id
        # A prepared batch might wait before execute, so recheck before spending
        # any provider budget as well as after the external execution.
        with self.engine.begin() as conn:
            stale = self._recheck(conn, snapshot)
            if stale is not None:
                return stale
        # Claim + owned immutable snapshot are now committed. No connection or
        # user/quiz lock remains while a provider or cached validator executes.
        failure = None
        if prepared.deterministic_json is not None:
            result = parse_json(prepared.deterministic_json) | {"provenance": snapshot.hashes | _versions(True) | {"saved_result_id": None}}
        elif not budget.remaining:
            failure = "AI_FAILED"
        else:
            try:
                gateway = self.gateway_factory(self.engine, settings=self.settings)
                gateway.executor = _BoundExecutor(gateway.executor, budget)
                evaluated = gateway.run(prepared.request, validator=VALIDATOR, provider=self.provider, mode=snapshot.mode)
                value = evaluated.data
                result = {"is_correct": value["score"] >= 85, "essay_score": value["score"], "feedback": value["feedback"],
                    "got_points": value["strengths"], "missing_points": value["missing_points"],
                    "grading_status": "graded" if evaluated.source == "live" else "fallback",
                    "provenance": snapshot.hashes | _versions(False) | {"saved_result_id": evaluated.saved_result_id}}
            except CleanupFailed:
                raise  # Worker process health failed: supervisor must replace it.
            except ApiError:
                failure = "AI_FAILED"
        with self.engine.begin() as conn:
            repo = GradingJobRepository(conn)
            stale = self._recheck(conn, snapshot)
            if stale is not None:
                return stale
            now = self.clock()
            args = {"owner": snapshot.owner, "token": snapshot.token, "revision": snapshot.revision, "now": now}
            if failure:
                changed = repo.fail(user_id, job_id, error_code=failure, **args)
                return GradingRun(str(job_id), "failed" if changed else "stale", error_code=failure)
            changed = repo.complete(user_id, job_id, result=result, **args)
            return GradingRun(str(job_id), "succeeded" if changed else "stale", result=result if changed else None)

    def run_next(self, *, stop_requested=lambda: False):
        with self.engine.connect() as conn:
            candidates = GradingJobRepository(conn).pending(now=self.clock())
        for candidate in candidates:
            if stop_requested():
                return GradingRun(None, "idle")
            try:
                outcome = self.run_job(candidate["user_id"], candidate["id"])
            except ApiError as exc:
                if exc.code in ("REQUEST_IN_PROGRESS", "NO_QUIZ"):
                    continue
                raise
            if outcome.state != "not_claimed":
                return outcome
        return GradingRun(None, "idle")
