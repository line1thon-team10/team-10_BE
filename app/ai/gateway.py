"""Owned, versioned AI results and a cancellable execution boundary."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import multiprocessing
import os
import select
import signal
import struct
import time
from uuid import UUID, uuid4

from app.ai.settings import AiSettings


KINDS = frozenset(("pdf_analysis", "question_pregenerate", "plan_generate", "plan_redo", "quiz_generate",
                   "essay_grade", "nl_parse", "tutor", "tutor_simplify", "proposal_explain", "reflection"))


class InvalidOutput(ValueError):
    """Valid JSON with an invalid structure, number, reference, or meaning."""


def validate_json(value) -> None:
    """Reject unsupported JSONB values, cycles and nonfinite numbers at any depth."""
    active = set()
    stack = [(value, False)]
    while stack:
        item, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        if type(item) is str:
            if "\x00" in item:
                raise InvalidOutput("JSONB cannot store a NUL character")
            continue
        if item is None or type(item) in (bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise InvalidOutput("Nonfinite JSON numbers are forbidden")
            continue
        if type(item) not in (dict, list):
            raise InvalidOutput("Only JSON values are accepted")
        if id(item) in active:
            raise InvalidOutput("Cyclic JSON values are forbidden")
        active.add(id(item))
        stack.append((item, True))
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise InvalidOutput("JSON object keys must be strings")
            if any("\x00" in key for key in item):
                raise InvalidOutput("JSONB object keys cannot contain a NUL character")
            stack.extend((child, False) for child in item.values())
        else:
            stack.extend((child, False) for child in item)


def canonical_json(value) -> str:
    validate_json(value)
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise InvalidOutput("The value cannot be encoded as finite JSON") from exc


def parse_json(raw: str):
    if type(raw) is not str:
        raise InvalidOutput("JSON text must be a string")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidOutput("Duplicate JSON keys are ambiguous")
            result[key] = value
        return result
    def constant(_):
        raise InvalidOutput("Nonfinite JSON numbers are forbidden")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    validate_json(value)
    return value


def _label(value, name):
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    if "\x00" in value:
        raise ValueError(f"{name} cannot contain a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


@dataclass(frozen=True, slots=True)
class MaterialRevision:
    material_id: str
    revision: str

    def __post_init__(self):
        object.__setattr__(self, "material_id", str(UUID(str(self.material_id))))
        _label(self.revision, "material revision")


@dataclass(frozen=True, slots=True, repr=False)
class ResultIdentity:
    kind: str
    provider: str
    model_version: str
    prompt_version: str
    schema_version: str
    input_json: str
    validation_json: str
    materials: tuple[MaterialRevision, ...] = ()
    scenario_key: str | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError("Unknown AI operation kind")
        for name in ("provider", "model_version", "prompt_version", "schema_version"):
            _label(getattr(self, name), name)
        object.__setattr__(self, "input_json", canonical_json(parse_json(self.input_json)))
        object.__setattr__(self, "validation_json", canonical_json(parse_json(self.validation_json)))
        materials = tuple(self.materials)
        if any(not isinstance(item, MaterialRevision) for item in materials):
            raise ValueError("MaterialRevision values are required")
        if len({item.material_id for item in materials}) != len(materials):
            raise ValueError("Material revisions cannot repeat a material ID")
        object.__setattr__(self, "materials", tuple(sorted(materials, key=lambda item: item.material_id)))
        if self.scenario_key is not None:
            _label(self.scenario_key, "scenario key")

    @classmethod
    def create(cls, *, kind, provider, model_version, prompt_version, schema_version, input,
               validation_context=None, materials=(), scenario_key=None):
        return cls(kind, provider, model_version, prompt_version, schema_version, canonical_json(input),
                   canonical_json(validation_context), tuple(materials), scenario_key)

    @property
    def input_hash(self):
        envelope = {"identityVersion": "ai-input-v1", "kind": self.kind, "provider": self.provider,
                    "model": self.model_version, "prompt": self.prompt_version, "schema": self.schema_version,
                    "input": parse_json(self.input_json), "validation": parse_json(self.validation_json),
                    "materials": [{"id": item.material_id, "revision": item.revision} for item in self.materials],
                    "scenario": self.scenario_key}
        return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DemoApproval:
    """A server-owned allowlist entry for one reviewed, self-authored artifact."""
    scenario_key: str
    input_hash: str
    self_authored: bool

    def __post_init__(self):
        _label(self.scenario_key, "scenario key")
        if type(self.input_hash) is not str or len(self.input_hash) != 64 or any(char not in "0123456789abcdef" for char in self.input_hash):
            raise ValueError("An exact identity hash is required")
        if self.self_authored is not True:
            raise ValueError("Shared results require explicit self-authored demo approval")

    def matches(self, identity: ResultIdentity):
        return self.scenario_key == identity.scenario_key and self.input_hash == identity.input_hash


@dataclass(frozen=True, slots=True, repr=False)
class GatewayRequest:
    owner_id: str
    identity: ResultIdentity
    job_id: str | None = None
    pdf_job: bool = False

    def __post_init__(self):
        object.__setattr__(self, "owner_id", str(UUID(str(self.owner_id))))
        if not isinstance(self.identity, ResultIdentity):
            raise ValueError("ResultIdentity is required")
        if self.job_id is not None:
            object.__setattr__(self, "job_id", str(UUID(str(self.job_id))))
        if type(self.pdf_job) is not bool or (self.pdf_job and self.identity.kind != "pdf_analysis"):
            raise ValueError("Only a background PDF analysis can select the PDF budget")


@dataclass(frozen=True, slots=True)
class FunctionRef:
    """Importable, trusted server code. Never construct this from client/AI input."""
    module: str
    name: str

    def __post_init__(self):
        if not all(type(part) is str and all(word.isidentifier() for word in part.split("."))
                   for part in (self.module, self.name)):
            raise ValueError("An importable module and function name are required")

    def resolve(self):
        value = importlib.import_module(self.module)
        for part in self.name.split("."):
            value = getattr(value, part)
        if not callable(value):
            raise TypeError("The configured reference is not callable")
        return value


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    function: FunctionRef
    sdk_max_retries: int = 0

    def __post_init__(self):
        if not isinstance(self.function, FunctionRef):
            raise TypeError("A trusted provider FunctionRef is required")
        if type(self.sdk_max_retries) is not int or self.sdk_max_retries != 0:
            raise ValueError("Disable SDK retries; only one gateway JSON syntax retry is allowed")


@dataclass(frozen=True, slots=True, repr=False)
class ProviderCall:
    identity: ResultIdentity
    format_retry: bool

    @property
    def input(self):
        return parse_json(self.identity.input_json)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One observed transport request, not one logical library invocation."""
    id: str
    provider: str
    model: str
    created_at: datetime
    status: str = "failed"
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    currency: str = "USD"
    latency_ms: int | None = None


class TelemetryError(ValueError):
    pass


def _usage_integer(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 2_147_483_647):
        raise TelemetryError("Usage counts and latency must be nonnegative database integers or null")
    return value


class _Telemetry:
    def __init__(self):
        self.records = {}
        self.open = set()

    def accept(self, event):
        if type(event) is not dict:
            raise TelemetryError("Invalid transport event")
        if event.get("type") == "request_started":
            if set(event) != {"type", "id", "provider", "model"}:
                raise TelemetryError("Invalid request start")
            try:
                request_id = str(UUID(event["id"]))
                provider, model = _label(event["provider"], "provider"), _label(event["model"], "model")
            except (ValueError, TypeError, AttributeError) as exc:
                raise TelemetryError("Invalid request identity") from exc
            if request_id != event["id"] or request_id in self.records:
                raise TelemetryError("Duplicate or noncanonical request identity")
            self.records[request_id] = UsageRecord(request_id, provider, model, datetime.now(timezone.utc))
            self.open.add(request_id)
            return
        if event.get("type") != "request_finished" or set(event) != {
            "type", "id", "status", "provider_request_id", "input_tokens", "output_tokens", "estimated_cost",
            "currency", "latency_ms",
        }:
            raise TelemetryError("Invalid request finish")
        request_id = event["id"]
        if type(request_id) is not str or request_id not in self.open:
            raise TelemetryError("A request must start and finish exactly once")
        if event["status"] not in ("succeeded", "failed"):
            raise TelemetryError("Invalid request status")
        provider_id = event["provider_request_id"]
        if provider_id is not None:
            try:
                _label(provider_id, "provider request ID")
            except ValueError as exc:
                raise TelemetryError("Invalid provider request ID") from exc
        cost, currency = event["estimated_cost"], event["currency"]
        try:
            if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
                raise TelemetryError("Unknown cost is null; known cost must be finite and nonnegative")
        except OverflowError as exc:
            raise TelemetryError("Cost cannot exceed finite numeric storage") from exc
        if type(currency) is not str or len(currency) != 3 or not all("A" <= char <= "Z" for char in currency):
            raise TelemetryError("A currency code is required")
        self.records[request_id] = replace(self.records[request_id], status=event["status"],
            provider_request_id=provider_id, input_tokens=_usage_integer(event["input_tokens"]),
            output_tokens=_usage_integer(event["output_tokens"]), estimated_cost=cost, currency=currency,
            latency_ms=_usage_integer(event["latency_ms"]))
        self.open.remove(request_id)

    @property
    def usage(self):
        # Started but unfinished transport attempts remain failed with unknown measurements.
        return tuple(self.records.values())


class ProviderContext:
    """Adapter contract at the actual transport boundary, including nested library calls.

    The adapter must disable SDK/transport retries and pass remaining_seconds to
    every request timeout. Hooks cannot automatically discover hidden network calls.
    Call start_request immediately before an actual send, after credential checks.
    """
    sdk_max_retries = 0

    def __init__(self, identity, deadline, emit):
        self.identity, self.deadline, self._emit = identity, deadline, emit
        self._started = {}
        self._completed = []

    @property
    def remaining_seconds(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The shared AI request budget is exhausted")
        return remaining

    def start_request(self, *, provider=None, model=None):
        self.remaining_seconds
        request_id = str(uuid4())
        self._started[request_id] = time.monotonic()
        self._emit({"type": "request_started", "id": request_id,
                    "provider": self.identity.provider if provider is None else provider,
                    "model": self.identity.model_version if model is None else model})
        return request_id

    def finish_request(self, request_id, *, status="succeeded", provider_request_id=None,
                       input_tokens=None, output_tokens=None, estimated_cost=None, currency="USD"):
        started = self._started.pop(request_id)
        self._emit({"type": "request_finished", "id": request_id, "status": status,
                    "provider_request_id": provider_request_id, "input_tokens": input_tokens,
                    "output_tokens": output_tokens, "estimated_cost": estimated_cost,
                    "currency": currency, "latency_ms": max(0, int((time.monotonic() - started) * 1000))})
        self._completed.append(status)


# Operational IPC guards, not PDF/product limits. Large or unbounded child output
# must not block the parent past its deadline or consume arbitrary parent memory.
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_MAX_TELEMETRY_EVENTS = 4096


def _emit_frame(connection, event):
    payload = canonical_json(event).encode("utf-8")
    if len(payload) > _MAX_FRAME_BYTES:
        raise InvalidOutput("AI output exceeds the process transport limit")
    frame = struct.pack("!Q", len(payload)) + payload
    while frame:
        written = os.write(connection.fileno(), frame[:65536])
        frame = frame[written:]


def _execution_child(connection, identity, provider, validator, cached_json, deadline):
    # POSIX session isolation lets timeout cleanup also stop adapter descendants.
    os.setsid()
    emit = lambda event: _emit_frame(connection, event)
    try:
        domain_validator = validator.resolve()
        if cached_json is None:
            invoke = provider.function.resolve()
            context = ProviderContext(identity, deadline, emit)
            for attempt in range(2):
                context.remaining_seconds
                completed_before = len(context._completed)
                raw = invoke(ProviderCall(identity, bool(attempt)), context)
                if context._started or "succeeded" not in context._completed[completed_before:]:
                    raise InvalidOutput("Each result attempt requires a completed successful transport request")
                try:
                    value = parse_json(raw) if type(raw) is str else raw
                    canonical_json(value)
                    break
                except json.JSONDecodeError:
                    if attempt:
                        raise
        else:
            value = parse_json(cached_json)
        if type(value) not in (dict, list):
            raise InvalidOutput("An AI result must be an object or array")
        validated = domain_validator(value, parse_json(identity.validation_json))
        if type(validated) not in (dict, list):
            raise InvalidOutput("A validator must return the validated object or array")
        canonical_json(validated)
        if time.monotonic() >= deadline:
            raise TimeoutError("The shared AI request budget is exhausted")
        emit({"type": "result", "data": validated})
    except BaseException:
        # Never return raw exception strings, prompts, credentials or stack traces.
        try:
            emit({"type": "failure"})
        except (OSError, ValueError):
            pass
    finally:
        connection.close()


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionResult:
    data: dict | list | None
    usage: tuple[UsageRecord, ...]
    failure: str | None


class CleanupFailed(RuntimeError):
    """Fatal worker-health error: the OS did not confirm process termination."""


class ProcessExecutor:
    """One process and one deadline for all requests, parsing, retries and validation."""
    def execute(self, identity: ResultIdentity, *, provider: ProviderSpec | None,
                validator: FunctionRef, deadline: float, cleanup_timeout: float, cached_json=None):
        if os.name != "posix":
            raise RuntimeError("The AI execution boundary requires POSIX process isolation")
        if not isinstance(validator, FunctionRef) or (cached_json is None and not isinstance(provider, ProviderSpec)):
            raise TypeError("Trusted provider and validator references are required")
        if not math.isfinite(deadline) or not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ValueError("Finite execution and cleanup budgets are required")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ExecutionResult(None, (), "timeout")
        # Reserve termination/join time inside the same budget (at most 10%).
        execution_deadline = deadline - min(cleanup_timeout, remaining / 10)
        receive, send = multiprocessing.get_context("spawn").Pipe(duplex=False)
        process = multiprocessing.get_context("spawn").Process(target=_execution_child,
            args=(send, identity, provider, validator, cached_json, execution_deadline), daemon=False)
        telemetry, buffer, result, failure, events = _Telemetry(), bytearray(), None, None, 0
        started = False
        try:
            process.start()
            started = True
            send.close()
            os.set_blocking(receive.fileno(), False)
            done = False
            while not done:
                remaining = execution_deadline - time.monotonic()
                if remaining <= 0:
                    failure = "timeout"
                    break
                ready, _, _ = select.select([receive.fileno()], [], [], remaining)
                if not ready:
                    failure = "timeout"
                    break
                chunk = os.read(receive.fileno(), 65536)
                if not chunk:
                    failure = "child_exit"
                    break
                buffer.extend(chunk)
                while len(buffer) >= 8:
                    size = struct.unpack("!Q", buffer[:8])[0]
                    if size > _MAX_FRAME_BYTES:
                        raise InvalidOutput("Oversized child frame")
                    if len(buffer) < size + 8:
                        break
                    event = parse_json(bytes(buffer[8:size + 8]).decode("utf-8"))
                    del buffer[:size + 8]
                    if type(event) is not dict:
                        raise TelemetryError("Invalid child event")
                    if event.get("type") == "result" and set(event) == {"type", "data"}:
                        if type(event["data"]) not in (dict, list):
                            raise InvalidOutput("Invalid child result")
                        if cached_json is None and (not any(row.status == "succeeded" for row in telemetry.usage) or telemetry.open):
                            raise TelemetryError("Live results require complete transport observations")
                        if cached_json is not None and telemetry.records:
                            raise TelemetryError("A cached validator cannot invoke the provider")
                        result, done = event["data"], True
                        break
                    if event == {"type": "failure"}:
                        failure, done = "invalid_or_failed", True
                        break
                    events += 1
                    if events > _MAX_TELEMETRY_EVENTS:
                        raise TelemetryError("Too many transport events")
                    telemetry.accept(event)
                    if time.monotonic() >= execution_deadline:
                        failure, done = "timeout", True
                        break
            if time.monotonic() >= execution_deadline:
                result, failure = None, "timeout"
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            result, failure = None, "invalid_or_failed"
        finally:
            send.close()
            receive.close()
            if started:
                # SIGKILL is deliberate: adapter code may ignore SIGTERM or block
                # in a native SDK. Never return while its process is still alive.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    if process.is_alive():
                        process.kill()
                except PermissionError:
                    # macOS can return EPERM for the group of an exited zombie.
                    # A live child still requires confirmed termination.
                    if process.is_alive():
                        process.kill()
                process.join(timeout=min(cleanup_timeout, max(0, deadline - time.monotonic())))
                if process.is_alive():
                    raise CleanupFailed("AI process termination was not confirmed; recycle this worker")
                process.close()
        return ExecutionResult(result, telemetry.usage, failure)


@dataclass(frozen=True, slots=True, repr=False)
class GatewayResult:
    data: dict | list
    saved_result_id: str
    source: str
    actual_call_count: int


def _ai_failed():
    from app.core.responses import ApiError
    return ApiError("AI_FAILED", "검증된 AI 결과를 준비하지 못했어요", 503)


class AiGateway:
    """Validated reuse or isolated provider execution, without domain state changes.

    The caller MUST commit existing answers and release its transactions/user locks
    before calling run(). This class only owns its short engine-created connections;
    it cannot release caller locks. Workers must recheck their material revision,
    answer revision and job lease before applying the returned result.
    """
    def __init__(self, engine, *, settings: AiSettings | None = None, approved_demos=()):
        from app.repositories.ai_results import AiResultRepository
        self.repository = AiResultRepository(engine)
        self.settings = AiSettings.from_environment() if settings is None else settings
        if not isinstance(self.settings, AiSettings):
            raise TypeError("AiSettings is required")
        self.approved_demos = tuple(approved_demos)
        if any(not isinstance(approval, DemoApproval) for approval in self.approved_demos):
            raise TypeError("Only explicit server-owned demo approvals are accepted")
        self.executor = ProcessExecutor()

    def _approval(self, identity):
        return next((approval for approval in self.approved_demos if approval.matches(identity)), None)

    def run(self, request: GatewayRequest, *, validator: FunctionRef, provider: ProviderSpec | None = None,
            mode: str = "live") -> GatewayResult:
        if not isinstance(request, GatewayRequest):
            raise TypeError("GatewayRequest is required")
        if mode not in ("live", "saved", "backup"):
            raise ValueError("Unknown AI result mode")
        deadline = time.monotonic() + self.settings.budget_for(pdf_job=request.pdf_job)
        cached = self.repository.lookup(request, demo_approval=self._approval(request.identity))
        # This branch intentionally precedes provider imports and credential checks.
        if cached is None and (mode in ("saved", "backup") or provider is None):
            raise _ai_failed()
        try:
            cached_json = canonical_json(cached.result) if cached else None
        except InvalidOutput:
            raise _ai_failed() from None
        outcome = self.executor.execute(request.identity, provider=None if cached else provider,
            validator=validator, deadline=deadline, cleanup_timeout=self.settings.cleanup_timeout_seconds,
            cached_json=cached_json)
        # These transactions begin only after the execution process was reaped.
        # Usage commits independently so a later cache write failure cannot erase it.
        self.repository.record_usage(request, outcome.usage)
        if outcome.failure is not None or outcome.data is None:
            raise _ai_failed()
        if cached:
            return GatewayResult(outcome.data, cached.id, cached.source, 0)
        result_id = self.repository.save(request, outcome.data)
        return GatewayResult(outcome.data, result_id, "live", len(outcome.usage))

    def register_demo(self, request: GatewayRequest, result, *, validator: FunctionRef) -> str:
        """Explicit server setup only; validate and publish one allowlisted demo."""
        if not isinstance(request, GatewayRequest):
            raise TypeError("GatewayRequest is required")
        approval = self._approval(request.identity)
        if approval is None or request.identity.materials:
            raise ValueError("Only an exact approved self-authored demo can be shared")
        # Establish ownership before validation; this connection closes immediately.
        self.repository.lookup(request, demo_approval=approval)
        deadline = time.monotonic() + self.settings.budget_for(pdf_job=request.pdf_job)
        outcome = self.executor.execute(request.identity, provider=None, validator=validator,
            deadline=deadline, cleanup_timeout=self.settings.cleanup_timeout_seconds, cached_json=canonical_json(result))
        if outcome.failure is not None or outcome.data is None:
            raise _ai_failed()
        return self.repository.save(request, outcome.data, demo_approval=approval)
