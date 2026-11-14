from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal
import multiprocessing
import os
from pathlib import Path
import signal
import struct
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.ai.gateway import (
    AiGateway, DemoApproval, ExecutionResult, FunctionRef, GatewayRequest, InvalidOutput, MaterialRevision, ProcessExecutor, ProviderSpec,
    ResultIdentity, TelemetryError, UsageRecord, _Telemetry, canonical_json, parse_json,
)
from app.ai.settings import AiSettings
from app.core.responses import ApiError
from app.db import schema as t
from app.domain.user import User
from app.repositories.ai_results import AiResultRepository
from app.repositories.users import UserRepository

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def identity(**changes):
    fields = {"kind": "nl_parse", "provider": "test-provider", "model_version": "test-model-v1",
              "prompt_version": "prompt-v1", "schema_version": "schema-v1", "input": {"text": "시험일을 알려 주세요"},
              "validation_context": {"allowed": ["examDate"]}}
    fields.update(changes)
    return ResultIdentity.create(**fields)


@pytest.fixture
def result_repository(domain_connection):
    conn = domain_connection
    if conn.dialect.name == "postgresql":
        schema = conn.scalar(text("select current_schema()"))
        args = {"options": f"-csearch_path={schema}"}
    else:
        args = {"check_same_thread": False}
    conn.commit()
    engine = create_engine(conn.engine.url, connect_args=args)
    if conn.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield AiResultRepository(engine)
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


@pytest.fixture
def gateway_user(domain_connection):
    user = User.anonymous(now=NOW)
    UserRepository(domain_connection).add(user)
    domain_connection.commit()
    return user.id


def add_material(conn, user_id):
    subject_id, material_id = str(uuid4()), str(uuid4())
    conn.execute(t.subjects.insert().values(id=subject_id, user_id=user_id, code=subject_id, name="과목", color="#123456",
                                          created_at=NOW, updated_at=NOW))
    conn.execute(t.materials.insert().values(id=material_id, subject_id=subject_id, original_name="자료.pdf", source_format="pdf",
                                           sha256="a" * 64, analysis_version="v1", created_at=NOW, updated_at=NOW))
    conn.commit()
    return material_id


def test_operational_budgets_have_separate_sync_and_pdf_defaults():
    settings = AiSettings.from_environment({})
    assert settings.budget_for() == 20
    assert settings.budget_for(pdf_job=True) == 300
    with pytest.raises(FrozenInstanceError):
        settings.request_budget_seconds = 30


def test_budget_environment_parsing_and_allowed_sync_upper_bound():
    settings = AiSettings.from_environment({"AI_REQUEST_BUDGET_SECONDS": "25", "AI_PDF_JOB_BUDGET_SECONDS": "180.5"})
    assert settings.request_budget_seconds == 25 and settings.pdf_job_budget_seconds == 180.5


@pytest.mark.parametrize("value", [0, -1, True, "", "   ", "NaN", "Infinity", "bad", None,
                                  Decimal("1e-1000"), Decimal("1e1000")])
def test_time_budgets_reject_unusable_or_nonfinite_values(value):
    for field in ("request_budget_seconds", "pdf_job_budget_seconds", "cleanup_timeout_seconds"):
        with pytest.raises(ValueError):
            AiSettings(**{field: value})


@pytest.mark.parametrize("value", [26, Decimal("25.000000000000000001")])
def test_sync_budget_checks_limit_before_float_conversion(value):
    with pytest.raises(ValueError):
        AiSettings(request_budget_seconds=value)


def test_pdf_budget_selection_does_not_accept_truthy_nonboolean_input():
    with pytest.raises(ValueError):
        AiSettings().budget_for(pdf_job="true")


def test_canonical_identity_normalizes_object_order_but_preserves_typed_and_ordered_inputs():
    first = identity(input={"b": [1, 2], "a": {"y": True, "x": "한글"}})
    second = identity(input={"a": {"x": "한글", "y": True}, "b": [1, 2]})
    assert first.input_hash == second.input_hash
    assert first.input_hash != identity(input={"b": [2, 1], "a": {"y": True, "x": "한글"}}).input_hash
    assert identity(input={"value": True}).input_hash != identity(input={"value": 1}).input_hash
    assert identity(input={"text": "답변"}).input_hash != identity(input={"text": "답변 "}).input_hash


@pytest.mark.parametrize("field,value", [("provider", "another"), ("model_version", "v2"), ("prompt_version", "v2"),
    ("schema_version", "v2"), ("kind", "tutor"), ("scenario_key", "demo:one"), ("validation_context", {"allowed": []})])
def test_cache_identity_includes_every_operation_and_validation_version(field, value):
    assert identity().input_hash != identity(**{field: value}).input_hash


def test_material_revision_identity_is_sorted_immutable_and_versioned():
    first, second = MaterialRevision(str(uuid4()), "r1"), MaterialRevision(str(uuid4()), "r2")
    key = identity(materials=[first, second])
    assert key.input_hash == identity(materials=[second, first]).input_hash
    assert key.input_hash != identity(materials=[replace(first, revision="changed"), second]).input_hash
    with pytest.raises(ValueError):
        identity(materials=[first, first])


@pytest.mark.parametrize("value", [{"nested": [float("nan")]}, {"nested": [float("inf")]}, {1: "key"},
                                  {"value": Decimal("1")}, {"value": (1, 2)}, {"text": "\ud800"},
                                  {"nested": ["text\x00more"]}, {"nested": [{"bad\x00key": 1}]}])
def test_json_validation_rejects_non_json_and_nonfinite_values_recursively(value):
    with pytest.raises(InvalidOutput):
        canonical_json(value)


def test_json_validation_detects_cycles_but_allows_reused_noncyclic_objects():
    item = {"value": 1}
    assert canonical_json([item, item]) == '[{"value":1},{"value":1}]'
    item["self"] = item
    with pytest.raises(InvalidOutput):
        canonical_json(item)


@pytest.mark.parametrize("raw", ['{"score":NaN}', '{"score":Infinity}', '{"score":1e999}', '{"a":1,"a":2}'])
def test_json_text_rejects_nonfinite_numbers_and_ambiguous_duplicate_keys(raw):
    with pytest.raises(InvalidOutput):
        parse_json(raw)


def test_private_result_reconnects_without_cross_user_reuse(result_repository, domain_connection, gateway_user):
    request = GatewayRequest(gateway_user, identity())
    result_id = result_repository.save(request, {"examDate": "2026-12-01"})
    reconnected = AiResultRepository(result_repository.engine).lookup(request)
    assert reconnected.id == result_id and reconnected.result == {"examDate": "2026-12-01"}
    assert reconnected.source == "private_cache"
    other = User.anonymous(now=NOW)
    UserRepository(domain_connection).add(other)
    domain_connection.commit()
    assert result_repository.lookup(GatewayRequest(other.id, request.identity)) is None
    assert result_repository.lookup(GatewayRequest(gateway_user, identity(prompt_version="v2"))) is None


def test_owner_and_material_chain_are_checked_before_cache_access(result_repository, domain_connection, gateway_user):
    material_id = add_material(domain_connection, gateway_user)
    request = GatewayRequest(gateway_user, identity(materials=[MaterialRevision(material_id, "structure-v1")]))
    result_repository.save(request, {"examDate": "2026-12-01"})
    assert result_repository.lookup(request) is not None
    assert result_repository.lookup(replace(request, identity=identity(materials=[MaterialRevision(material_id, "structure-v2")]))) is None
    other = User.anonymous(now=NOW)
    UserRepository(domain_connection).add(other)
    domain_connection.commit()
    with pytest.raises(ApiError) as error:
        result_repository.lookup(GatewayRequest(other.id, request.identity))
    assert error.value.code == "NO_MATERIAL" and error.value.status == 404


def test_shared_demo_requires_exact_server_approval_and_cannot_publish_private_material(result_repository, domain_connection, gateway_user):
    request = GatewayRequest(gateway_user, identity(scenario_key="self-authored:one"))
    approval = DemoApproval(request.identity.scenario_key, request.identity.input_hash, True)
    result_id = result_repository.save(request, {"examDate": "2026-12-01"}, demo_approval=approval)
    assert result_repository.lookup(request) is None
    shared = result_repository.lookup(request, demo_approval=approval)
    assert shared.id == result_id and shared.source == "demo_cache"
    mismatch = DemoApproval("self-authored:other", request.identity.input_hash, True)
    assert result_repository.lookup(request, demo_approval=mismatch) is None
    with pytest.raises(ValueError):
        result_repository.save(request, {}, demo_approval=mismatch)
    material_id = add_material(domain_connection, gateway_user)
    private = replace(request, identity=identity(scenario_key="self-authored:one", materials=[MaterialRevision(material_id, "v1")]))
    with pytest.raises(ValueError):
        result_repository.save(private, {}, demo_approval=DemoApproval(private.identity.scenario_key, private.identity.input_hash, True))


def test_private_result_has_priority_over_an_approved_demo_result(result_repository, gateway_user):
    request = GatewayRequest(gateway_user, identity(scenario_key="self-authored:one"))
    approval = DemoApproval(request.identity.scenario_key, request.identity.input_hash, True)
    result_repository.save(request, {"source": "demo"}, demo_approval=approval)
    private_id = result_repository.save(request, {"source": "private"})
    assert result_repository.lookup(request, demo_approval=approval).id == private_id


def test_invalid_result_is_not_persisted_and_caller_connection_is_not_taken(result_repository, domain_connection, gateway_user):
    request = GatewayRequest(gateway_user, identity())
    for invalid in (None, "answer", {"score": float("nan")}):
        with pytest.raises(ValueError):
            result_repository.save(request, invalid)
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 0
    with pytest.raises(TypeError):
        AiResultRepository(domain_connection)
    assert not domain_connection.closed
    assert domain_connection.scalar(select(func.count()).select_from(t.users)) == 1


# These are deterministic fake transports. No test in this module calls an AI service.
# Module-level functions can be imported by the spawned execution process.
def fake_provider(call, context):
    payload = call.input
    behavior = payload.get("behavior", "valid")
    if behavior == "missing_credentials":
        raise RuntimeError("secret credential details must not escape")
    if behavior == "unobserved":
        return {"page": 2, "evidence": "source"}
    assert context.sdk_max_retries == 0 and context.remaining_seconds > 0
    request_id = context.start_request()
    if "pid_file" in payload:
        Path(payload["pid_file"]).write_text(str(os.getpid()))
    if behavior == "crash":
        os._exit(7)
    if behavior == "exception":
        raise RuntimeError("secret provider response must not escape")
    if behavior == "slow":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(payload["delay"])
        Path(payload["late_file"]).write_text("unwanted late side effect")
    time.sleep(payload.get("request_delay", 0))
    if behavior == "malformed_telemetry":
        context.finish_request(request_id, input_tokens=True)
    elif behavior == "forged_telemetry":
        context.finish_request(str(uuid4()))
    else:
        context.finish_request(request_id, **payload.get("measurements", {}))
    if behavior == "multiple":
        second = context.start_request(model="nested-model")
        context.finish_request(second, input_tokens=10, output_tokens=2, provider_request_id="observed-2")
    if behavior == "syntax_always" or behavior == "syntax_once" and not call.format_retry:
        return '{"page":'
    if behavior == "semantic":
        return payload["raw"]
    if behavior == "large":
        return {"page": 2, "evidence": "source", "text": "x" * payload["size"]}
    return {"page": 2, "evidence": "source"}


def fake_validator(value, context):
    if context.get("delay"):
        time.sleep(context["delay"])
    if type(value) is not dict or type(value.get("page")) is not int or value["page"] not in context["pages"]:
        raise InvalidOutput("The referenced page is outside the selected range")
    if value.get("evidence") != context["evidence"]:
        raise InvalidOutput("The evidence is not supported by the material snapshot")
    return value


def nul_request_id_provider(call, context):
    request_id = context.start_request()
    context.finish_request(request_id, provider_request_id="request\x00id")
    return {"page": 2, "evidence": "source"}


PROVIDER = ProviderSpec(FunctionRef(__name__, "fake_provider"))
VALIDATOR = FunctionRef(__name__, "fake_validator")


def execute_fake(payload=None, *, context=None, budget=4, cached=None):
    key = identity(input=payload or {}, validation_context=context or {"pages": [1, 2], "evidence": "source"})
    result = ProcessExecutor().execute(key, provider=PROVIDER, validator=VALIDATOR,
                                      deadline=time.monotonic() + budget, cleanup_timeout=0.5,
                                      cached_json=cached)
    assert not multiprocessing.active_children()
    return result


def test_process_validates_result_and_reports_only_observed_transport_calls():
    output = execute_fake()
    assert output.failure is None and output.data == {"page": 2, "evidence": "source"}
    assert len(output.usage) == 1 and output.usage[0].status == "succeeded"
    assert output.usage[0].input_tokens is None and output.usage[0].estimated_cost is None
    assert output.usage[0].latency_ms >= 0


def test_nested_library_calls_are_individually_observed_with_unknown_metrics_left_null():
    output = execute_fake({"behavior": "multiple"})
    assert output.failure is None and len(output.usage) == 2
    assert output.usage[0].input_tokens is None
    assert output.usage[1].input_tokens == 10 and output.usage[1].model == "nested-model"
    assert output.usage[1].provider_request_id == "observed-2" and output.usage[1].estimated_cost is None


@pytest.mark.parametrize("behavior,succeeds", [("syntax_once", True), ("syntax_always", False)])
def test_only_json_syntax_errors_receive_one_retry(behavior, succeeds):
    output = execute_fake({"behavior": behavior})
    assert (output.failure is None) is succeeds
    assert len(output.usage) == 2


@pytest.mark.parametrize("raw", ['{"page":NaN}', '{"page":1e999}', '{"page":2,"page":1}', 'null',
                                  '{"page":true,"evidence":"source"}', '{"page":3,"evidence":"source"}',
                                  '{"page":2,"evidence":"invented"}', '{"nested":[{"value":Infinity}]}'])
def test_semantic_structure_evidence_range_and_nonfinite_errors_never_retry(raw):
    output = execute_fake({"behavior": "semantic", "raw": raw})
    assert output.data is None and output.failure == "invalid_or_failed"
    assert len(output.usage) == 1  # A successful transport may still carry an invalid answer.
    assert output.usage[0].status == "succeeded"


@pytest.mark.parametrize("behavior,count", [("missing_credentials", 0), ("unobserved", 0), ("crash", 1),
                                          ("exception", 1), ("malformed_telemetry", 1), ("forged_telemetry", 1)])
def test_provider_failures_and_invalid_telemetry_are_fail_closed_and_reaped(behavior, count):
    output = execute_fake({"behavior": behavior})
    assert output.data is None and output.failure is not None
    assert len(output.usage) == count and all(row.status == "failed" for row in output.usage)


def test_timeout_kills_sigterm_ignoring_provider_and_does_not_leave_late_work(tmp_path):
    pid_file, late_file = tmp_path / "pid", tmp_path / "late"
    started = time.monotonic()
    output = execute_fake({"behavior": "slow", "delay": 1.5, "pid_file": str(pid_file), "late_file": str(late_file)}, budget=1)
    assert output.data is None and output.failure == "timeout" and len(output.usage) == 1
    assert output.usage[0].status == "failed" and output.usage[0].latency_ms is None
    assert time.monotonic() - started < 1.2
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not late_file.exists()


def test_json_retry_uses_remaining_budget_instead_of_starting_a_new_timeout():
    started = time.monotonic()
    output = execute_fake({"behavior": "syntax_once", "request_delay": 0.65}, budget=1.5)
    assert output.data is None and output.failure == "timeout"
    assert len(output.usage) == 2 and output.usage[0].status == "succeeded" and output.usage[1].status == "failed"
    assert time.monotonic() - started < 1.7


def test_domain_validation_is_inside_the_same_killable_budget():
    output = execute_fake(context={"pages": [1, 2], "evidence": "source", "delay": 2}, budget=1)
    assert output.data is None and output.failure == "timeout"
    assert len(output.usage) == 1 and output.usage[0].status == "succeeded"


def test_cached_validation_has_zero_transport_calls_and_rejects_stale_invalid_data():
    output = execute_fake(cached='{"page":2,"evidence":"source"}')
    assert output.data == {"page": 2, "evidence": "source"} and output.usage == ()
    bad = execute_fake(cached='{"page":7,"evidence":"source"}')
    assert bad.data is None and bad.usage == ()


def test_large_output_does_not_deadlock_pipe_or_defeat_deadline():
    valid = execute_fake({"behavior": "large", "size": 2 * 1024 * 1024})
    assert valid.failure is None and len(valid.data["text"]) == 2 * 1024 * 1024
    oversized = execute_fake({"behavior": "large", "size": 9 * 1024 * 1024})
    assert oversized.data is None and oversized.failure is not None and len(oversized.usage) == 1


def partial_frame_child(connection, *args):
    os.setsid()
    os.write(connection.fileno(), struct.pack("!Q", 1024 * 1024) + b'{"type":')
    time.sleep(10)


def test_partial_large_frame_cannot_block_parent_receive_beyond_deadline(monkeypatch):
    from app.ai import gateway
    monkeypatch.setattr(gateway, "_execution_child", partial_frame_child)
    started = time.monotonic()
    output = execute_fake(budget=1)
    assert output.failure == "timeout" and output.data is None
    assert time.monotonic() - started < 1.2


def test_failed_transport_cannot_be_disguised_as_success_with_a_fabricated_answer():
    output = execute_fake({"measurements": {"status": "failed"}})
    assert output.failure is not None and output.data is None
    assert len(output.usage) == 1 and output.usage[0].status == "failed"


def test_failed_transport_with_malformed_body_does_not_trigger_json_retry():
    output = execute_fake({"behavior": "syntax_always", "measurements": {"status": "failed"}})
    assert output.data is None and output.failure is not None
    assert len(output.usage) == 1 and output.usage[0].status == "failed"


@pytest.mark.parametrize("retries", [1, 6, True, None])
def test_sdk_automatic_retries_are_forbidden(retries):
    with pytest.raises(ValueError):
        ProviderSpec(PROVIDER.function, sdk_max_retries=retries)


@pytest.mark.parametrize("change", [{"input_tokens": -1}, {"output_tokens": True}, {"input_tokens": 2 ** 32},
    {"estimated_cost": float("nan")}, {"estimated_cost": -1}, {"currency": "usd"}, {"provider_request_id": ""},
    {"status": "pending"}, {"latency_ms": 0.1}, {"untrusted_owner": "id"}])
def test_telemetry_rejects_invalid_measurements_without_fabricating_values(change):
    collector, request_id = _Telemetry(), str(uuid4())
    collector.accept({"type": "request_started", "id": request_id, "provider": "test", "model": "v1"})
    event = {"type": "request_finished", "id": request_id, "status": "succeeded", "provider_request_id": None,
             "input_tokens": None, "output_tokens": None, "estimated_cost": None, "currency": "USD", "latency_ms": 2}
    event.update(change)
    with pytest.raises(TelemetryError):
        collector.accept(event)
    assert collector.usage[0].status == "failed" and collector.usage[0].input_tokens is None


def test_telemetry_rejects_duplicate_start_or_finish_and_forged_request_ids():
    collector, request_id = _Telemetry(), str(uuid4())
    start = {"type": "request_started", "id": request_id, "provider": "test", "model": "v1"}
    collector.accept(start)
    with pytest.raises(TelemetryError):
        collector.accept(start)
    end = {"type": "request_finished", "id": request_id, "status": "succeeded", "provider_request_id": None,
           "input_tokens": 3, "output_tokens": 1, "estimated_cost": 0.0001, "currency": "USD", "latency_ms": 0}
    collector.accept(end)
    assert collector.usage[0].estimated_cost == 0.0001
    for invalid in (end, {**end, "id": str(uuid4())}):
        with pytest.raises(TelemetryError):
            collector.accept(invalid)


def add_job(conn, owner_id, *, kind="nl_parse"):
    job_id = str(uuid4())
    conn.execute(t.ai_jobs.insert().values(id=job_id, user_id=owner_id, kind=kind, status="running",
        idempotency_key=job_id, input_hash="a" * 64, input_payload={}, schema_version="schema-v1", created_at=NOW))
    conn.commit()
    return job_id


def usage_rows(conn):
    conn.commit()
    return list(conn.execute(select(t.ai_usage_logs).order_by(t.ai_usage_logs.c.created_at, t.ai_usage_logs.c.id)).mappings())


def test_usage_persists_actual_attempts_with_unknown_measurements_null(result_repository, domain_connection, gateway_user):
    job_id = add_job(domain_connection, gateway_user)
    request = GatewayRequest(gateway_user, identity(), job_id=job_id)
    unknown = UsageRecord(str(uuid4()), "provider", "model", NOW)
    known = UsageRecord(str(uuid4()), "provider", "nested-model", NOW, status="succeeded",
                        input_tokens=10, output_tokens=3, estimated_cost=0.0001, provider_request_id="actual-id", latency_ms=20)
    result_repository.record_usage(request, [unknown, known])
    rows = {row["id"]: row for row in usage_rows(domain_connection)}
    assert len(rows) == 2 and {row["user_id"] for row in rows.values()} == {gateway_user}
    assert {row["job_id"] for row in rows.values()} == {job_id}
    assert rows[unknown.id]["input_tokens"] is None and rows[unknown.id]["estimated_cost"] is None
    assert rows[unknown.id]["latency_ms"] is None and rows[unknown.id]["status"] == "failed"
    assert rows[known.id]["estimated_cost"] == pytest.approx(0.0001)
    assert rows[known.id]["input_tokens"] == 10 and rows[known.id]["provider_request_id"] == "actual-id"


def test_usage_survives_material_or_job_deletion_during_external_work(result_repository, domain_connection, gateway_user):
    material_id, job_id = add_material(domain_connection, gateway_user), add_job(domain_connection, gateway_user)
    request = GatewayRequest(gateway_user, identity(materials=[MaterialRevision(material_id, "v1")]), job_id=job_id)
    assert result_repository.lookup(request) is None
    domain_connection.execute(t.materials.delete().where(t.materials.c.id == material_id))
    domain_connection.execute(t.ai_jobs.delete().where(t.ai_jobs.c.id == job_id))
    domain_connection.commit()
    result_repository.record_usage(request, [UsageRecord(str(uuid4()), "provider", "model", NOW)])
    rows = usage_rows(domain_connection)
    assert len(rows) == 1 and rows[0]["user_id"] == gateway_user and rows[0]["job_id"] is None
    with pytest.raises(ApiError):
        result_repository.save(request, {"result": "material disappeared"})


def test_usage_survives_job_deletion_after_lookup_before_insert(result_repository, domain_connection, gateway_user):
    job_id = add_job(domain_connection, gateway_user)
    request = GatewayRequest(gateway_user, identity(), job_id=job_id)
    records = [UsageRecord(str(uuid4()), "provider", "model", NOW, status="succeeded", input_tokens=10),
               UsageRecord(str(uuid4()), "provider", "model", NOW)]
    deleted = []

    def delete_job(_conn, _cursor, _statement, _parameters, context, _many):
        if context.isinsert and context.compiled.statement.table.name == "ai_usage_logs" and not deleted:
            deleted.append(job_id)
            with result_repository.engine.begin() as concurrent:
                concurrent.execute(t.ai_jobs.delete().where(t.ai_jobs.c.id == job_id))

    event.listen(result_repository.engine, "before_cursor_execute", delete_job)
    try:
        result_repository.record_usage(request, records)
    finally:
        event.remove(result_repository.engine, "before_cursor_execute", delete_job)
    rows = {row["id"]: row for row in usage_rows(domain_connection)}
    assert deleted == [job_id] and set(rows) == {record.id for record in records}
    assert all(row["job_id"] is None for row in rows.values())
    assert rows[records[0].id]["status"] == "succeeded" and rows[records[0].id]["input_tokens"] == 10
    assert rows[records[1].id]["status"] == "failed" and rows[records[1].id]["input_tokens"] is None


def test_unrelated_integrity_failure_rolls_back_the_whole_usage_batch(result_repository, domain_connection, gateway_user):
    job_id = add_job(domain_connection, gateway_user)
    request = GatewayRequest(gateway_user, identity(), job_id=job_id)
    existing = UsageRecord(str(uuid4()), "provider", "model", NOW)
    result_repository.record_usage(request, [existing])
    fresh = UsageRecord(str(uuid4()), "provider", "model", NOW)
    with pytest.raises(IntegrityError):
        result_repository.record_usage(request, [fresh, existing])
    rows = usage_rows(domain_connection)
    assert len(rows) == 1 and rows[0]["id"] == existing.id and rows[0]["job_id"] == job_id


def test_job_ownership_and_kind_are_required_before_using_saved_results(result_repository, domain_connection, gateway_user):
    other = User.anonymous(now=NOW)
    UserRepository(domain_connection).add(other)
    domain_connection.commit()
    foreign_job = add_job(domain_connection, other.id)
    wrong_kind = add_job(domain_connection, gateway_user, kind="tutor")
    for job_id in (foreign_job, wrong_kind, str(uuid4())):
        with pytest.raises(ValueError):
            result_repository.lookup(GatewayRequest(gateway_user, identity(), job_id=job_id))


def test_usage_validation_prevents_partial_or_fabricated_measurements(result_repository, domain_connection, gateway_user):
    valid = UsageRecord(str(uuid4()), "provider", "model", NOW)
    invalid = replace(valid, id=str(uuid4()), input_tokens=-2)
    request = GatewayRequest(gateway_user, identity())
    with pytest.raises(TelemetryError):
        result_repository.record_usage(request, [valid, invalid])
    assert usage_rows(domain_connection) == []
    result_repository.record_usage(request, [])
    assert usage_rows(domain_connection) == []


def fake_request(owner, payload=None, **identity_changes):
    return GatewayRequest(owner, identity(input=payload or {}, validation_context={"pages": [1, 2], "evidence": "source"},
                                          **identity_changes))


def test_gateway_live_validates_saves_and_reconnects_keylessly(result_repository, domain_connection, gateway_user):
    gateway = AiGateway(result_repository.engine)
    request = fake_request(gateway_user, {"behavior": "multiple"})
    first = gateway.run(request, provider=PROVIDER, validator=VALIDATOR)
    assert first.source == "live" and first.actual_call_count == 2
    assert first.data == {"page": 2, "evidence": "source"}
    reconnected = AiGateway(result_repository.engine)
    for mode in ("live", "saved", "backup"):
        saved = reconnected.run(request, validator=VALIDATOR, mode=mode)
        assert saved.saved_result_id == first.saved_result_id and saved.source == "private_cache"
        assert saved.data == first.data and saved.actual_call_count == 0
    assert len(usage_rows(domain_connection)) == 2
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 1


@pytest.mark.parametrize("mode", ["saved", "backup"])
def test_forced_saved_miss_never_initializes_provider_or_checks_credentials(result_repository, domain_connection, gateway_user, monkeypatch, mode):
    gateway = AiGateway(result_repository.engine)
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved miss cannot execute provider code")
    monkeypatch.setattr(gateway.executor, "execute", forbidden)
    with pytest.raises(ApiError) as error:
        gateway.run(fake_request(gateway_user), provider="invalid unused provider", validator=None, mode=mode)
    assert (error.value.code, error.value.status) == ("AI_FAILED", 503)
    assert usage_rows(domain_connection) == []


def test_live_keyless_miss_and_provider_credential_error_never_create_usage(result_repository, domain_connection, gateway_user):
    gateway = AiGateway(result_repository.engine)
    for provider in (None, PROVIDER):
        with pytest.raises(ApiError) as error:
            gateway.run(fake_request(gateway_user, {"behavior": "missing_credentials"}), provider=provider, validator=VALIDATOR)
        assert error.value.code == "AI_FAILED" and error.value.status == 503
        assert "secret" not in error.value.message
    assert usage_rows(domain_connection) == []


@pytest.mark.parametrize("changes", [{"input": {"changed": True}}, {"provider": "other"}, {"model_version": "v2"},
                                    {"prompt_version": "v2"}, {"schema_version": "v2"},
                                    {"validation_context": {"pages": [1], "evidence": "source"}}])
def test_gateway_saved_reuse_requires_all_input_and_version_dimensions(result_repository, domain_connection, gateway_user, changes):
    request = fake_request(gateway_user)
    result_repository.save(request, {"page": 2, "evidence": "source"})
    fields = {"input": {}, "validation_context": {"pages": [1, 2], "evidence": "source"}, **changes}
    different = GatewayRequest(gateway_user, identity(**fields))
    with pytest.raises(ApiError) as error:
        AiGateway(result_repository.engine).run(different, validator=VALIDATOR, mode="saved")
    assert error.value.code == "AI_FAILED" and usage_rows(domain_connection) == []


def test_gateway_cache_does_not_cross_owners_or_material_revisions(result_repository, domain_connection, gateway_user):
    material_id = add_material(domain_connection, gateway_user)
    request = fake_request(gateway_user, materials=[MaterialRevision(material_id, "v1")])
    result_repository.save(request, {"page": 2, "evidence": "source"})
    gateway = AiGateway(result_repository.engine)
    changed = replace(request, identity=replace(request.identity, materials=(MaterialRevision(material_id, "v2"),)))
    with pytest.raises(ApiError) as error:
        gateway.run(changed, validator=VALIDATOR, mode="saved")
    assert error.value.code == "AI_FAILED"
    other = User.anonymous(now=NOW)
    UserRepository(domain_connection).add(other)
    domain_connection.commit()
    with pytest.raises(ApiError) as error:
        gateway.run(replace(request, owner_id=other.id), validator=VALIDATOR, mode="saved")
    assert error.value.code == "NO_MATERIAL"
    private = fake_request(gateway_user)
    result_repository.save(private, {"page": 2, "evidence": "source"})
    with pytest.raises(ApiError) as error:
        gateway.run(replace(private, owner_id=other.id), validator=VALIDATOR, mode="saved")
    assert error.value.code == "AI_FAILED" and usage_rows(domain_connection) == []


def test_invalid_saved_result_is_revalidated_without_a_live_fallback(result_repository, domain_connection, gateway_user):
    request = fake_request(gateway_user)
    result_repository.save(request, {"page": 99, "evidence": "source"})
    with pytest.raises(ApiError) as error:
        AiGateway(result_repository.engine).run(request, validator=VALIDATOR, provider=PROVIDER)
    assert error.value.code == "AI_FAILED" and usage_rows(domain_connection) == []


@pytest.mark.parametrize("behavior,calls,status", [("syntax_always", 2, "succeeded"), ("exception", 1, "failed"),
                                                 ("malformed_telemetry", 1, "failed")])
def test_gateway_failure_persists_only_actual_observations_and_no_result(result_repository, domain_connection, gateway_user, behavior, calls, status):
    with pytest.raises(ApiError) as error:
        AiGateway(result_repository.engine).run(fake_request(gateway_user, {"behavior": behavior}), provider=PROVIDER, validator=VALIDATOR)
    assert error.value.code == "AI_FAILED"
    rows = usage_rows(domain_connection)
    assert len(rows) == calls and {row["status"] for row in rows} == {status}
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 0


@pytest.mark.parametrize("raw", [
    '{"page":2,"evidence":"source","extra":["text\\u0000more"]}',
    '{"page":2,"evidence":"source","extra":{"bad\\u0000key":1}}',
])
def test_jsonb_unsupported_provider_text_is_ai_failure_and_keeps_usage(result_repository, domain_connection, gateway_user, raw):
    request = fake_request(gateway_user, {"behavior": "semantic", "raw": raw})
    with pytest.raises(ApiError) as error:
        AiGateway(result_repository.engine).run(request, provider=PROVIDER, validator=VALIDATOR)
    assert error.value.code == "AI_FAILED" and error.value.status == 503
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 0
    rows = usage_rows(domain_connection)
    assert len(rows) == 1 and rows[0]["status"] == "succeeded"


@pytest.mark.parametrize("value", [{"answer": "text\x00more"}, {"bad\x00key": "answer"}])
def test_repository_rejects_unsupported_text_before_database_write(result_repository, domain_connection, gateway_user, value):
    with pytest.raises(InvalidOutput):
        result_repository.save(GatewayRequest(gateway_user, identity()), value)
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 0


def test_unsupported_provider_request_id_is_failed_observation_without_database_error(result_repository, domain_connection, gateway_user):
    provider = ProviderSpec(FunctionRef(__name__, "nul_request_id_provider"))
    with pytest.raises(ApiError) as error:
        AiGateway(result_repository.engine).run(fake_request(gateway_user), provider=provider, validator=VALIDATOR)
    assert error.value.code == "AI_FAILED" and error.value.status == 503
    rows = usage_rows(domain_connection)
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["provider_request_id"] is None
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 0



def test_gateway_own_connections_are_closed_during_external_work_and_usage_survives_save_failure(result_repository, domain_connection, gateway_user, monkeypatch):
    gateway = AiGateway(result_repository.engine)
    original_execute = gateway.executor.execute
    def inspect_connections(*args, **kwargs):
        assert gateway.repository.engine.pool.checkedout() == 0
        # Another connection can write while the execution boundary is active.
        with gateway.repository.engine.begin() as conn:
            conn.execute(t.users.update().where(t.users.c.id == gateway_user).values(updated_at=NOW))
        return original_execute(*args, **kwargs)
    def fail_save(*args, **kwargs):
        raise RuntimeError("synthetic cache failure")
    monkeypatch.setattr(gateway.executor, "execute", inspect_connections)
    monkeypatch.setattr(gateway.repository, "save", fail_save)
    with pytest.raises(RuntimeError, match="synthetic cache failure"):
        gateway.run(fake_request(gateway_user), provider=PROVIDER, validator=VALIDATOR)
    assert len(usage_rows(domain_connection)) == 1
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 0
    assert not domain_connection.closed


def test_shared_demo_is_explicitly_validated_allowlisted_and_keyless(result_repository, domain_connection, gateway_user):
    request = fake_request(gateway_user, scenario_key="self-authored:approved")
    approval = DemoApproval(request.identity.scenario_key, request.identity.input_hash, True)
    gateway = AiGateway(result_repository.engine, approved_demos=[approval])
    result_id = gateway.register_demo(request, {"page": 2, "evidence": "source"}, validator=VALIDATOR)
    domain_connection.commit()
    assert domain_connection.scalar(select(t.ai_saved_results.c.user_id).where(t.ai_saved_results.c.id == result_id)) is None
    other = User.anonymous(now=NOW)
    UserRepository(domain_connection).add(other)
    domain_connection.commit()
    shared = gateway.run(replace(request, owner_id=other.id), validator=VALIDATOR, mode="backup")
    assert shared.source == "demo_cache" and shared.saved_result_id == result_id and shared.actual_call_count == 0
    with pytest.raises(ApiError):
        AiGateway(result_repository.engine).run(request, validator=VALIDATOR, mode="saved")
    with pytest.raises(ValueError):
        AiGateway(result_repository.engine).register_demo(request, {}, validator=VALIDATOR)
    with pytest.raises(ApiError):
        gateway.register_demo(request, {"page": 50, "evidence": "source"}, validator=VALIDATOR)
    assert domain_connection.scalar(select(func.count()).select_from(t.ai_saved_results)) == 1
    assert usage_rows(domain_connection) == []


def test_pdf_budget_requires_explicit_background_selection(result_repository, gateway_user, monkeypatch):
    request = fake_request(gateway_user, kind="pdf_analysis")
    result_repository.save(request, {"page": 2, "evidence": "source"})
    gateway = AiGateway(result_repository.engine, settings=AiSettings(request_budget_seconds=2, pdf_job_budget_seconds=7))
    budgets = []
    def observe_budget(*args, **kwargs):
        budgets.append(kwargs["deadline"] - time.monotonic())
        return ExecutionResult({"page": 2, "evidence": "source"}, (), None)
    monkeypatch.setattr(gateway.executor, "execute", observe_budget)
    gateway.run(request, validator=VALIDATOR, mode="saved")
    gateway.run(replace(request, pdf_job=True), validator=VALIDATOR, mode="saved")
    assert 1 < budgets[0] <= 2 and 6 < budgets[1] <= 7
    with pytest.raises(ValueError):
        replace(fake_request(gateway_user), pdf_job=True)
