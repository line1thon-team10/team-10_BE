from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, text

from app.ai.gateway import (
    DemoApproval, GatewayRequest, InvalidOutput, MaterialRevision, ResultIdentity, canonical_json, parse_json,
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
                                  {"value": Decimal("1")}, {"value": (1, 2)}, {"text": "\ud800"}])
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
