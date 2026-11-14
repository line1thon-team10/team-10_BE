"""Owned, versioned AI results and a cancellable execution boundary."""
from dataclasses import dataclass
import hashlib
import json
import math
from uuid import UUID


KINDS = frozenset(("pdf_analysis", "question_pregenerate", "plan_generate", "plan_redo", "quiz_generate",
                   "essay_grade", "nl_parse", "tutor", "tutor_simplify", "proposal_explain", "reflection"))


class InvalidOutput(ValueError):
    """Valid JSON with an invalid structure, number, reference, or meaning."""


def validate_json(value) -> None:
    """Reject non-JSON values, cycles and nonfinite numbers at any depth."""
    active = set()
    stack = [(value, False)]
    while stack:
        item, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        if item is None or type(item) in (str, bool, int):
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
