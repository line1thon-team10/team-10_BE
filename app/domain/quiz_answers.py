"""Input invariants shared by HTTP and durable answer writes."""
from app.core.responses import ApiError


def validate_answer_text(value: str | None) -> str | None:
    # Both PostgreSQL text/JSON and UTF-8 hashes must represent the same input.
    # Preserve whitespace and all valid Unicode without normalization.
    if value is not None and any(char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ApiError("INVALID_INPUT", "답안에 저장할 수 없는 문자가 있어요", 422)
    return value
