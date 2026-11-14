"""Fake HTTP transport through the real pinned prompt builder, never live AI."""
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.ai import grading_adapter as adapter
from app.ai.gateway import (FunctionRef, InvalidOutput, ProcessExecutor, ProviderCall, ProviderContext,
                            ProviderSpec, ResultIdentity)


def evidence():
    material_id = str(uuid4())
    return {"question_id": str(uuid4()), "materials": [{"id": material_id, "page_count": 10}],
            "sources": [{"material_id": material_id, "start_page": 2, "end_page": 3, "quote": "실제 원문 인용"}]}


def grade(score=85):
    return {"score": score, "strengths": ["핵심 설명"], "missing_points": [], "feedback": "좋은 설명입니다."}


def identity(behavior="valid", *, context=None):
    return ResultIdentity.create(kind="essay_grade", provider="groq", model_version=adapter.MODEL_VERSION,
        prompt_version=adapter.PROMPT_VERSION, schema_version=adapter.SCHEMA_VERSION,
        input={"answer": behavior, "question": {"id": "original-question", "question": "개념을 설명하세요.",
              "answer": "모범 답안", "source_page": 2, "source_text": "실제 원문 인용"}},
        validation_context=context or evidence())


def fake_provider(call, context):
    """Importable child fixture; use the actual pinned grade_essay with fake SDK I/O."""
    behavior = call.input["answer"]
    if behavior == "syntax":
        raw = json.dumps(grade()) if call.format_retry else "{broken"
    elif behavior == "semantic":
        raw = json.dumps(grade(101))
    elif behavior == "both_bad":
        raw = "{broken"
    else:
        raw = json.dumps(grade())
    def factory(timeout):
        def send(**kwargs):
            if behavior == "failure":
                raise RuntimeError("fake transport failure")
            return SimpleNamespace(id="fake-request", usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)), close=lambda: None)
    return adapter.essay_provider(call, context, client_factory=factory)


@pytest.mark.parametrize("score", [0, 84, 85, 100])
def test_strict_essay_score_boundaries(score):
    assert adapter.validate_essay_result(grade(score), evidence())["score"] == score


@pytest.mark.parametrize("changes", [{"score": value} for value in (-1, 101, True, 84.9, "85", None, float("nan"))]
    + [{"feedback": ""}, {"feedback": "\x00"}, {"feedback": "\ud800"}, {"strengths": [1]},
       {"missing_points": "x"}, {"source_page": 999}, {"is_correct": True}])
def test_semantic_structure_and_storage_errors_are_not_valid_results(changes):
    with pytest.raises(InvalidOutput):
        adapter.validate_essay_result({**grade(), **changes}, evidence())


@pytest.mark.parametrize("field,value", [("start_page", 0), ("end_page", 11), ("start_page", True),
    ("end_page", 1), ("quote", None), ("quote", " "), ("material_id", str(uuid4()))])
def test_evidence_bounds_and_actual_quote_are_required(field, value):
    context = evidence()
    context["sources"][0][field] = value
    with pytest.raises(InvalidOutput):
        adapter.validate_essay_result(grade(), context)


def test_real_pinned_grade_essay_builds_prompt_but_does_not_parse_raw_response(monkeypatch):
    from app.ai import engine
    pinned = engine.module("essay_grader")
    observed = []
    original = pinned.grade_essay
    def wrapped(question, answer, groq_service=None):
        observed.append(groq_service)
        return original(question, answer, groq_service=groq_service)
    monkeypatch.setattr(pinned, "grade_essay", wrapped)
    monkeypatch.setattr(pinned, "GroqService", lambda: pytest.fail("The unbounded vendor client must never initialize"))
    sent, events, closed = [], [], []
    def send(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(id="request-1", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4),
            choices=[SimpleNamespace(message=SimpleNamespace(content="{ malformed raw JSON"))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)), close=lambda: closed.append(True))
    ident = identity()
    context = ProviderContext(ident, time.monotonic() + 5, events.append)
    raw = adapter.essay_provider(ProviderCall(ident, True), context, client_factory=lambda timeout: client)
    assert raw == "{ malformed raw JSON" and len(observed) == 1 and observed[0] is not None and closed == [True]
    assert sent[0]["model"] == adapter.MODEL_VERSION and 0 < sent[0]["timeout"] <= 5
    assert sent[0]["response_format"]["json_schema"]["schema"] == pinned.ESSAY_SCHEMA
    assert "모범 답안" in sent[0]["messages"][1]["content"] and "실제 원문 인용" in sent[0]["messages"][1]["content"]
    assert len(sent[0]["messages"]) == 3
    assert [entry["type"] for entry in events] == ["request_started", "request_finished"]
    assert events[-1]["status"] == "succeeded" and events[-1]["input_tokens"] == 10 and events[-1]["output_tokens"] == 4
    assert events[-1]["estimated_cost"] is None


@pytest.mark.parametrize("behavior,calls,success", [("syntax", 2, True), ("both_bad", 2, False),
    ("semantic", 1, False), ("failure", 1, False), ("valid", 1, True)])
def test_gateway_retries_only_raw_json_syntax_with_real_vendor_injection(behavior, calls, success):
    result = ProcessExecutor().execute(identity(behavior), provider=ProviderSpec(FunctionRef(__name__, "fake_provider")),
        validator=adapter.VALIDATOR, deadline=time.monotonic() + 10, cleanup_timeout=1)
    assert len(result.usage) == calls
    assert (result.failure is None) is success
    assert (result.data == grade()) is success
    assert all(row.status == ("failed" if behavior == "failure" else "succeeded") for row in result.usage)
    assert all(row.input_tokens is None and row.output_tokens is None and row.estimated_cost is None for row in result.usage)


def test_missing_key_does_not_initialize_sdk_or_report_a_request(monkeypatch):
    import groq
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(groq, "Groq", lambda **kwargs: pytest.fail("No SDK initialization without credentials"))
    events, ident = [], identity()
    with pytest.raises(RuntimeError, match="not configured"):
        adapter.essay_provider(ProviderCall(ident, False), ProviderContext(ident, time.monotonic() + 5, events.append))
    assert events == []


@pytest.mark.parametrize("status", [200, 302, 429, 500])
def test_actual_groq_sdk_has_zero_retries_and_receives_remaining_timeout(monkeypatch, status):
    import groq
    original = groq.Groq
    calls, settings, events = [], [], []
    def transport(request):
        calls.append(request)
        body = {"id": "mock-sdk", "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(grade())},
            "finish_reason": "stop"}], "created": 0, "model": adapter.MODEL_VERSION, "object": "chat.completion"}
        return httpx.Response(status, json=body if status == 200 else {"error": {"message": "mock failure"}},
            headers={"location": "https://redirect-must-not-be-followed.invalid/"}, request=request)
    def factory(**kwargs):
        settings.append(kwargs.copy())
        configured_client = kwargs.pop("http_client")
        assert configured_client.follow_redirects is False
        configured_client.close()
        return original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False))
    monkeypatch.setattr(groq, "Groq", factory)
    monkeypatch.setenv("GROQ_API_KEY", "fake-local-test-key")
    ident = identity()
    context = ProviderContext(ident, time.monotonic() + 5, events.append)
    if status == 200:
        assert json.loads(adapter.essay_provider(ProviderCall(ident, False), context)) == grade()
    else:
        with pytest.raises(groq.APIStatusError):
            adapter.essay_provider(ProviderCall(ident, False), context)
    assert len(calls) == 1 and settings[0]["max_retries"] == 0
    assert 0 < settings[0]["timeout"] <= 5
    assert all(0 < value <= settings[0]["timeout"] for value in calls[0].extensions["timeout"].values())
    assert events[-1]["status"] == ("succeeded" if status == 200 else "failed")
