"""Pinned essay prompts with one observed, non-retrying Groq transport per attempt."""
import os
from uuid import UUID

from app.ai.gateway import FunctionRef, InvalidOutput, ProviderSpec, canonical_json


MODEL_VERSION = "openai/gpt-oss-120b"
PROMPT_VERSION = "essay-6d74ffb6c455a306e912252013d76f93e30a6210-adapter-v1"
SCHEMA_VERSION = "essay-grade-v1"
PROVIDER = ProviderSpec(FunctionRef("app.ai.grading_adapter", "essay_provider"), sdk_max_retries=0)
VALIDATOR = FunctionRef("app.ai.grading_adapter", "validate_essay_result")


def validate_evidence(context):
    """Ownership is established by the DB snapshot; validate its page references."""
    canonical_json(context)
    if type(context) is not dict:
        raise InvalidOutput("An owned evidence snapshot is required")
    try:
        UUID(context["question_id"])
        materials = context["materials"]
        sources = context["sources"]
        if type(materials) is not list or type(sources) is not list or not sources:
            raise ValueError
        counts = {}
        for material in materials:
            material_id = str(UUID(material["id"]))
            if material_id in counts:
                raise ValueError
            counts[material_id] = material["page_count"]
        has_quote = False
        for source in sources:
            count = counts[str(UUID(source["material_id"]))]
            start, end, quote = source["start_page"], source["end_page"], source["quote"]
            if any(type(value) is not int for value in (count, start, end)) or not 1 <= start <= end <= count:
                raise ValueError
            if quote is not None and type(quote) is not str:
                raise ValueError
            has_quote = has_quote or bool(quote and quote.strip())
        if not has_quote:
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise InvalidOutput("Essay evidence must refer to owned material pages and actual quoted text") from None


def validate_essay_result(value, context):
    validate_evidence(context)
    canonical_json(value)
    if type(value) is not dict or set(value) != {"score", "strengths", "missing_points", "feedback"}:
        raise InvalidOutput("Unexpected essay result structure")
    if type(value["score"]) is not int or not 0 <= value["score"] <= 100:
        raise InvalidOutput("An essay score must be an integer from zero to one hundred")
    for name in ("strengths", "missing_points"):
        if type(value[name]) is not list or any(type(text) is not str for text in value[name]):
            raise InvalidOutput("Essay feedback points must be text arrays")
    if type(value["feedback"]) is not str or not value["feedback"].strip():
        raise InvalidOutput("Essay feedback must not be blank")
    return value


def _groq_client(timeout_seconds):
    # Only the live child execution reaches here. Saved/backup paths do not even
    # import a provider or initialize credentials.
    key = os.environ.get("GROQ_API_KEY")
    if not key or not key.strip():
        raise RuntimeError("Essay provider is not configured")
    from groq import Groq
    return Groq(api_key=key, max_retries=0, timeout=timeout_seconds)


class _RawResponse(Exception):
    def __init__(self, raw):
        self.raw = raw


class _ObservedGroq:
    """Injected into the pinned prompt builder, never its six-retry GroqService."""
    def __init__(self, client, context, *, format_retry):
        self.client, self.context, self.format_retry = client, context, format_retry

    def generate(self, messages, schema, name, max_tokens=1200):
        if self.format_retry:
            messages = [*messages, {"role": "system", "content": "올바른 JSON 객체만 반환하세요. 코드 블록은 쓰지 마세요."}]
        timeout = self.context.remaining_seconds
        request_id = self.context.start_request(provider="groq", model=self.context.identity.model_version)
        try:
            response = self.client.chat.completions.create(
                model=self.context.identity.model_version, messages=messages, temperature=0.1,
                max_completion_tokens=max_tokens, timeout=timeout,
                response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            )
        except BaseException:
            self.context.finish_request(request_id, status="failed")
            raise
        usage = getattr(response, "usage", None)
        self.context.finish_request(request_id, status="succeeded", provider_request_id=getattr(response, "id", None),
            input_tokens=getattr(usage, "prompt_tokens", None), output_tokens=getattr(usage, "completion_tokens", None))
        raw = response.choices[0].message.content
        if type(raw) is not str:
            raise InvalidOutput("The provider returned no JSON text")
        # The pinned grade_essay expects an already parsed dict. Capture its raw
        # transport response first, so gateway owns JSON syntax retry/validation.
        # Semantic failures must never be converted into format retries.
        raise _RawResponse(raw)


def essay_provider(call, context, *, client_factory=None):
    from app.ai import engine
    payload = call.input
    answer = payload["answer"]
    if type(answer) is not str or not answer.strip():
        raise InvalidOutput("Blank answers are graded deterministically before AI execution")
    client = (client_factory or _groq_client)(context.remaining_seconds)
    try:
        injected = _ObservedGroq(client, context, format_retry=call.format_retry)
        try:
            engine.module("essay_grader").grade_essay(payload["question"], answer, groq_service=injected)
        except _RawResponse as captured:
            return captured.raw
        raise InvalidOutput("The pinned essay grader did not use the observed transport")
    finally:
        client.close()
