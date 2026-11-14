"""Durable preview primitives; callers own the surrounding transaction."""
import hashlib
import json
import math


class InvalidProposalJSON(ValueError):
    """The input cannot be stored losslessly in both SQLite and PostgreSQL."""


def _json_string(value: str) -> str:
    if "\x00" in value:
        raise InvalidProposalJSON("JSON strings and keys must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidProposalJSON("JSON strings and keys must be valid Unicode") from None
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidProposalJSON("Duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise InvalidProposalJSON("JSON numbers must be finite")


def canonical_object(value: dict | str) -> dict:
    """Validate/copy an object, or parse raw JSON before duplicate keys are lost.

    Whitespace within strings and array ordering are preserved. Domain-specific
    normalization (for example sorting a set of dependency IDs) is the caller's
    responsibility, not an implicit change to the recorded input.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        except (json.JSONDecodeError, RecursionError):
            raise InvalidProposalJSON("A valid JSON object is required") from None
    if not isinstance(value, dict):
        raise InvalidProposalJSON("A JSON object is required")
    active = set()

    def visit(item):
        if item is None or type(item) in (bool, int):
            return item
        if isinstance(item, str):
            return _json_string(item)
        if type(item) is float:
            if not math.isfinite(item):
                raise InvalidProposalJSON("JSON numbers must be finite")
            return item
        if isinstance(item, (dict, list)):
            if id(item) in active:
                raise InvalidProposalJSON("Circular JSON objects are not allowed")
            active.add(id(item))
            try:
                if isinstance(item, list):
                    return [visit(child) for child in item]
                result = {}
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise InvalidProposalJSON("JSON object keys must be strings")
                    result[_json_string(key)] = visit(child)
                return result
            finally:
                active.remove(id(item))
        raise InvalidProposalJSON("Only JSON object, array, string, boolean, finite number or null values are allowed")

    try:
        result = visit(value)
        # Also reject oversized integers/depth unsupported by the JSON encoder.
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result
    except (RecursionError, OverflowError):
        raise InvalidProposalJSON("JSON nesting or numbers exceed the supported representation") from None


def canonical_hash(value: dict | str) -> str:
    encoded = json.dumps(canonical_object(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
