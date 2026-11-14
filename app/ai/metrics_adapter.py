"""Translate owned DB snapshots into deterministic mastery facts, without AI calls."""
from datetime import datetime, timezone
import hashlib
import json

from app.domain.mastery import ConfirmedAttempt, RecoveryEntry, UnitScope, calculate_mastery


KINDS = {"daily": "daily", "extra": "more", "more": "more", "practice": "custom", "custom": "custom", "retry": "retry", "review": "retry"}


def _index(rows, label):
    result = {}
    for row in rows:
        if row["id"] in result and result[row["id"]] != row:
            raise ValueError(f"Conflicting {label} snapshots")
        result[row["id"]] = row
    return result


def _db_time(value):
    if not isinstance(value, datetime):
        raise ValueError("A stored timestamp is required")
    # Application writes use UTC; SQLite drops DateTime timezone metadata.
    return value.replace(tzinfo=timezone.utc) if value.utcoffset() is None else value


def current_scope(unit, concepts) -> UnitScope:
    """Hash scope/source content only, excluding changing mastery timestamps."""
    snapshot = {"version": "mastery-scope-v1", "material": unit["material_id"], "unit": unit["id"],
        "sha256": unit["material_sha256"], "analysis": unit["analysis_version"],
        "quantityBasis": unit["quantity_basis"], "start": unit["start_page"], "end": unit["end_page"],
        "inScope": unit["in_scope"], "concepts": [
            {"id": row["id"], "start": row["start_page"], "end": row["end_page"], "inScope": row["in_scope"]}
            for row in sorted(concepts, key=lambda row: row["id"])
            if row["unit_id"] == unit["id"] and row["retired_at"] is None]}
    revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return UnitScope(unit["material_id"], unit["id"], revision)


def calculate_learning_metrics(data, *, eligibility=()):
    """No code aliases, vendor calls, inferred supplements, or unfinalized answers."""
    units, concepts, questions = (_index(data[name], name) for name in ("units", "concepts", "questions"))
    for concept in concepts.values():
        if concept["unit_id"] not in units:
            raise ValueError("A concept references an unavailable unit")
    for question in questions.values():
        if question["unit_id"] not in units:
            raise ValueError("A question references an unavailable unit")
        if question["concept_id"] is not None:
            concept = concepts.get(question["concept_id"])
            if concept is None or concept["unit_id"] != question["unit_id"]:
                raise ValueError("A question concept belongs to another unit")
    attempts = []
    for row in data["attempts"]:
        if (row["finalized_at"] is None or row["graded_at"] is None or row["grading_status"] not in ("graded", "fallback")
                or row["session_status"] not in ("submitted", "carried_over") or row["session_finalized_at"] is None
                or row["session_grading_status"] != "completed"):
            raise ValueError("Only finalized, graded set results can affect mastery")
        question = questions.get(row["question_id"])
        if question is None:
            raise ValueError("A confirmed attempt references an unavailable question")
        unit = units[question["unit_id"]]
        if row["quiz_type"] not in KINDS:
            raise ValueError("Unknown confirmed quiz kind")
        attempts.append(ConfirmedAttempt(row["id"], row["quiz_item_id"], question["id"], unit["material_id"],
            unit["id"], question["concept_id"], row["answered_date"], _db_time(row["answered_at"]),
            _db_time(row["finalized_at"]), question["difficulty"], row["confidence"], row["is_correct"], KINDS[row["quiz_type"]]))
    entries = []
    for row in data.get("recovery_entries", ()):
        unit = units.get(row["unit_id"])
        try:
            score = int(row["score"])
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("A recovery entry requires a finite integer score") from exc
        if unit is None or type(row["score"]) is bool or row["score"] != score:
            raise ValueError("A recovery entry must reference an observed unit and integer score")
        entries.append(RecoveryEntry(unit["material_id"], unit["id"], row["recorded_date"], score,
                                     row["id"], _db_time(row["created_at"])))
    scopes = [current_scope(unit, concepts.values()) for unit in units.values()]
    return calculate_mastery(attempts, recovery_entries=entries, current_scopes=scopes, eligibility=eligibility)
