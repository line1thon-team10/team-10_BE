"""S1 goals: nullable answers and explicit confirmation are separate facts."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import math
from typing import Any, Literal
from zoneinfo import ZoneInfo


CONFIRMATION_COLUMNS = {
    "examDate": "onboarding_exam_date_confirmed_at",
    "goal": "onboarding_goal_confirmed_at",
    "goalTotal": "onboarding_goal_total_confirmed_at",
    "goalEtc": "onboarding_goal_etc_confirmed_at",
}
GOAL_GROUPS = {
    "examDate": ("exam_date",),
    "goal": ("raw_goal", "target_grade", "target_score"),
    "goalTotal": ("passing_criteria", "grade_criteria"),
    "goalEtc": ("grading_weights", "emphasized_content", "other_exam_info"),
}
GOAL_COLUMNS = frozenset(column for columns in GOAL_GROUPS.values() for column in columns)


@dataclass(frozen=True)
class GoalUpdate:
    """Validated interpretation supplied by O-03; this module never parses text."""
    subject_id: str
    values: Mapping[str, Any] = field(default_factory=dict)
    confirmed_fields: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationMessage:
    role: Literal["user", "ai"]
    text: str
    chips: tuple[str, ...] = ()

    def validated(self) -> dict:
        if self.role not in ("user", "ai"):
            raise ValueError("Invalid conversation role")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Conversation text is required")
        if not isinstance(self.chips, (tuple, list)) or any(
            not isinstance(chip, str) or not chip.strip() for chip in self.chips
        ):
            raise ValueError("Chips must contain nonempty strings")
        if self.role == "user" and self.chips:
            raise ValueError("Only an AI response can provide chips")
        return {"role": self.role, "text": self.text, "chips": list(self.chips)}


def _text(value: str | None) -> str | None:
    return (value.strip() or None) if value is not None else None


def _joined(row: Mapping, *columns: str) -> str | None:
    # Older data can keep the same information in several structured columns.
    values = dict.fromkeys(value for column in columns if (value := _text(row.get(column))))
    return "\n".join(values) or None


def subject_view(row: Mapping) -> dict:
    confirmed = {field: row.get(column) is not None for field, column in CONFIRMATION_COLUMNS.items()}
    goal = _text(row.get("target_grade"))
    exam_date = row.get("exam_date")
    complete = all(confirmed.values()) and type(exam_date) is date and goal is not None
    started = any(confirmed.values()) or any(row.get(column) is not None for column in GOAL_COLUMNS)
    return {
        "id": row["id"], "name": row["name"],
        "examDate": exam_date.isoformat() if exam_date else None,
        "goal": goal, "goalTotal": _joined(row, *GOAL_GROUPS["goalTotal"]),
        "goalEtc": _joined(row, *GOAL_GROUPS["goalEtc"]), "target": row.get("target_score"),
        "status": "confirmed" if complete else "asking" if started else "pending",
        "confirmedFields": confirmed,
    }


def goal_changes(row: Mapping, update: GoalUpdate, at: datetime) -> dict:
    """Invalidate only the changed group; explicit optional 'none' may confirm."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Confirmation time must include a timezone")
    if not update.values and not update.confirmed_fields:
        raise ValueError("A goal update must contain a value or confirmation")
    if set(update.values) - GOAL_COLUMNS or set(update.confirmed_fields) - CONFIRMATION_COLUMNS.keys():
        raise ValueError("Unknown goal field")
    values = {}
    for column, value in update.values.items():
        if value is not None:
            if column == "exam_date":
                if type(value) is not date:
                    raise ValueError("Exam date must be a date")
            elif column == "target_score":
                if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                    raise ValueError("Target must be a number")
                # Check exact Decimal bounds before float rounding/underflow.
                if isinstance(value, Decimal) and not value.is_finite():
                    raise ValueError("Target must be finite and in 0..100")
                if not 0 <= value <= 100:
                    raise ValueError("Target must be finite and in 0..100")
                try:
                    value = float(value)
                except (OverflowError, ValueError) as exc:
                    raise ValueError("Target must be finite and in 0..100") from exc
                if not math.isfinite(value) or not 0 <= value <= 100:
                    raise ValueError("Target must be finite and in 0..100")
            elif not isinstance(value, str):
                raise ValueError("Goal text must be a string")
            else:
                value = _text(value)
        if row.get(column) != value:
            values[column] = value
    for group, columns in GOAL_GROUPS.items():
        if set(values) & set(columns):
            values[CONFIRMATION_COLUMNS[group]] = None
    merged = {**row, **values}
    for group, confirmed in update.confirmed_fields.items():
        if type(confirmed) is not bool:
            raise ValueError("Confirmation must be a boolean")
        if confirmed and group == "examDate" and type(merged.get("exam_date")) is not date:
            raise ValueError("A valid exam date is required before confirmation")
        if confirmed and group == "goal" and not _text(merged.get("target_grade")):
            raise ValueError("A goal is required before confirmation")
        column = CONFIRMATION_COLUMNS[group]
        values[column] = (merged.get(column) or at) if confirmed else None
    merged.update(values)
    if subject_view(merged)["status"] == "confirmed":
        changed = any(row.get(column) != value for column, value in values.items())
        values["goal_confirmed_at"] = at if changed else row.get("goal_confirmed_at") or at
    else:
        values["goal_confirmed_at"] = None
    values = {column: value for column, value in values.items() if row.get(column) != value}
    if values:
        values["updated_at"] = at
    return values


def event_view(row: Mapping, timezone_name: str) -> dict:
    """Read the weekly/one-off Event shape shared by S1 and the later O-02 API."""
    tz = ZoneInfo(timezone_name)
    def local(value):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(tz)
    start, end = local(row["starts_at"]), local(row["ends_at"])
    days = None
    if row["recurrence_rule"]:
        rule = dict(part.split("=", 1) for part in row["recurrence_rule"].split(";"))
        weekdays = {day: i for i, day in enumerate(("MO", "TU", "WE", "TH", "FR", "SA", "SU"), 1)}
        if rule.get("FREQ") != "WEEKLY" or not rule.get("BYDAY"):
            raise ValueError("Unsupported stored recurrence")
        days = sorted({weekdays[day] for day in rule["BYDAY"].split(",")})
    return {
        "id": row["id"], "kind": row["event_type"], "title": row["title"], "days": days,
        "date": None if days else start.date().isoformat(), "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"), "added": False,
    }
