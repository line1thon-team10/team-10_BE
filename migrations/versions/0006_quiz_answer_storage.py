"""Persist answer revisions, grading leases and sealed result snapshots.

Run online to backfill legacy rows. SQL-only rendering describes DDL, not the
Python data migration. Legacy answers/results are preserved, never guessed.
Downgrade removes only these new storage tables; back up new drafts first.
"""
import hashlib
import json
from datetime import date, datetime
from uuid import NAMESPACE_URL, uuid5

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_quiz_answer_storage"
down_revision = "0005_onboarding_state"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(JSONB(), "postgresql")
UUID = sa.Uuid(as_uuid=False)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _json(value):
    def convert(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        return float(item)
    return json.loads(json.dumps(value, default=convert, ensure_ascii=False, allow_nan=False))


def _legacy(conn):
    names = ("quiz_sessions", "quiz_items", "quiz_attempts", "questions", "question_sources",
             "mastery_history", "units", "materials", "subjects")
    metadata = sa.MetaData()
    tables = {name: sa.Table(name, metadata, autoload_with=conn) for name in names}
    # Reflection sees SQLite UUIDs as CHAR(32), PostgreSQL as UUID objects.
    # Apply the original application's canonical string representation to both.
    for table in tables.values():
        for column in table.c:
            if column.name == "id" or column.name.endswith("_id"):
                column.type = sa.Uuid(as_uuid=False)
    rows = {name: [dict(row) for row in conn.execute(sa.select(table)).mappings()]
            for name, table in tables.items()}
    keyed = {name: {row["id"]: row for row in data} for name, data in rows.items()}
    for item in rows["quiz_items"]:
        session = keyed["quiz_sessions"][item["quiz_session_id"]]
        question = keyed["questions"][item["question_id"]]
        unit = keyed["units"][question["unit_id"]]
        material = keyed["materials"][unit["material_id"]]
        subject = keyed["subjects"][material["subject_id"]]
        if (session["user_id"] != subject["user_id"] or
                (session["subject_id"] is not None and session["subject_id"] != subject["id"])):
            raise RuntimeError("퀴즈 문항 소유권 불일치: 기존 데이터를 보존하고 migration을 중단합니다.")
    for attempt in rows["quiz_attempts"]:
        if attempt["confidence"] not in ("sure", "unsure") or (
                attempt["essay_score"] is not None and not 0 <= attempt["essay_score"] <= 100):
            raise RuntimeError("퀴즈 답안 확신도/점수 오류: 기존 데이터를 보존하고 migration을 중단합니다.")
    _json(rows)  # serialization must also succeed before any SQLite DDL
    return rows, keyed


def _create_tables():
    answers = op.create_table(
        "quiz_answers",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("quiz_item_id", UUID, sa.ForeignKey("quiz_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("choice", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(), nullable=False),
        sa.Column("answer_hash", sa.String(), nullable=True),
        sa.Column("input_recovered", sa.Boolean(), nullable=False),
        sa.Column("legacy_answer", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_date", sa.Date(), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_quiz_answer_revision"),
        sa.CheckConstraint("confidence IN ('sure', 'unsure')", name="ck_quiz_answer_confidence"),
        sa.CheckConstraint("choice IS NULL OR choice >= 0", name="ck_quiz_answer_choice"),
        sa.CheckConstraint("(input_recovered = true AND answer_hash IS NOT NULL AND ((choice IS NOT NULL AND text IS NULL) OR (choice IS NULL AND text IS NOT NULL))) OR (input_recovered = false AND legacy_answer IS NOT NULL AND choice IS NULL AND text IS NULL AND answer_hash IS NULL)", name="ck_quiz_answer_payload"),
        sa.CheckConstraint("answer_hash IS NULL OR length(answer_hash) = 64", name="ck_quiz_answer_hash"),
    )
    op.create_index("ix_quiz_answers_1", "quiz_answers", ["quiz_item_id", "revision"], unique=True)
    heads = op.create_table(
        "quiz_answer_heads",
        sa.Column("quiz_item_id", UUID, sa.ForeignKey("quiz_items.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("answer_id", UUID, sa.ForeignKey("quiz_answers.id", ondelete="CASCADE"), unique=True, nullable=False),
    )
    op.create_table(
        "quiz_answer_receipts",
        sa.Column("quiz_item_id", UUID, sa.ForeignKey("quiz_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_revision", sa.Integer(), nullable=False),
        sa.Column("answer_hash", sa.String(), nullable=False),
        sa.Column("response_answer_id", UUID, sa.ForeignKey("quiz_answers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("quiz_item_id", "request_revision", "answer_hash"),
        sa.CheckConstraint("request_revision >= 0", name="ck_quiz_receipt_revision"),
        sa.CheckConstraint("length(answer_hash) = 64", name="ck_quiz_receipt_hash"),
    )
    jobs = op.create_table(
        "quiz_grading_jobs",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("answer_id", UUID, sa.ForeignKey("quiz_answers.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("answer_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_token", UUID, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_count", sa.Integer(), nullable=False),
        sa.Column("result", JSON, nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("answer_revision > 0 AND claim_count >= 0", name="ck_quiz_job_revision"),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_quiz_job_status"),
        sa.CheckConstraint("(status = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at > claimed_at) OR (status <> 'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)", name="ck_quiz_job_lease"),
        sa.CheckConstraint("(status = 'succeeded' AND result IS NOT NULL) OR (status <> 'succeeded' AND result IS NULL)", name="ck_quiz_job_result"),
    )
    op.create_index("ix_quiz_grading_jobs_1", "quiz_grading_jobs", ["status", "lease_expires_at", "created_at"])
    states = op.create_table(
        "quiz_submission_states",
        sa.Column("quiz_session_id", UUID, sa.ForeignKey("quiz_sessions.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answer_set_hash", sa.String(), nullable=True),
        sa.Column("input_recovered", sa.Boolean(), nullable=False),
        sa.Column("sealed_answers", JSON, nullable=False),
        sa.Column("legacy", sa.Boolean(), nullable=False),
        sa.Column("legacy_snapshot", JSON, nullable=True),
        sa.Column("result_snapshot", JSON, nullable=True),
        sa.Column("result_provenance", sa.String(), nullable=True),
        sa.Column("result_schema_version", sa.String(), nullable=True),
        sa.Column("result_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("(answer_set_hash IS NULL OR length(answer_set_hash) = 64) AND (input_recovered = false OR answer_set_hash IS NOT NULL)", name="ck_quiz_submission_hash"),
        sa.CheckConstraint("(result_snapshot IS NULL AND result_provenance IS NULL AND result_schema_version IS NULL AND result_captured_at IS NULL) OR (result_snapshot IS NOT NULL AND result_provenance IS NOT NULL AND result_provenance IN ('submitted', 'legacy_reconstructed') AND result_schema_version IS NOT NULL AND result_captured_at IS NOT NULL AND result_captured_at >= sealed_at)", name="ck_quiz_snapshot_metadata"),
    )
    return answers, heads, jobs, states


def _backfill(conn, rows, keyed, answers, heads, jobs, states):
    by_item = {}
    for attempt in rows["quiz_attempts"]:
        item = keyed["quiz_items"][attempt["quiz_item_id"]]
        question = keyed["questions"][item["question_id"]]
        choice, text, recovered = None, None, True
        if question["type"] in ("choice", "ox"):
            matches = [index for index, option in enumerate(question["choices"] or [])
                       if isinstance(option, dict) and option.get("text") == attempt["answer"]]
            recovered = len(matches) == 1
            if recovered:
                choice = matches[0]
        elif question["type"] in ("short", "essay"):
            text = attempt["answer"]
        else:
            recovered = False
        answer = {
            "id": str(uuid5(NAMESPACE_URL, f"team10:quiz-answer:{attempt['id']}:1")),
            "quiz_item_id": item["id"], "revision": 1, "choice": choice, "text": text,
            "confidence": attempt["confidence"], "input_recovered": recovered,
            "answer_hash": _hash({"choice": choice, "text": text, "confidence": attempt["confidence"]}) if recovered else None,
            "legacy_answer": attempt["answer"], "answered_at": attempt["answered_at"],
            "answered_date": attempt["answered_date"],
        }
        conn.execute(answers.insert().values(**answer))
        conn.execute(heads.insert().values(quiz_item_id=item["id"], answer_id=answer["id"]))
        by_item[item["id"]] = answer
        succeeded = attempt["is_correct"] is not None and attempt["grading_status"] in ("graded", "fallback")
        job = {
            "id": str(uuid5(NAMESPACE_URL, f"team10:quiz-grading:{attempt['id']}:1")),
            "answer_id": answer["id"], "answer_revision": 1, "claim_count": 0,
            "status": "succeeded" if succeeded else "failed" if attempt["grading_status"] == "failed" else "queued",
            "created_at": attempt["answered_at"], "finished_at": attempt["graded_at"] if succeeded else None,
        }
        if succeeded:
            job["result"] = {name: attempt[name] for name in
                             ("is_correct", "essay_score", "feedback", "got_points", "missing_points", "grading_status")}
        conn.execute(jobs.insert().values(**job))
    for session in rows["quiz_sessions"]:
        if session["status"] not in ("submitted", "carried_over") and session["finalized_at"] is None:
            continue
        items = [item for item in rows["quiz_items"] if item["quiz_session_id"] == session["id"]]
        item_ids = {item["id"] for item in items}
        question_ids = {item["question_id"] for item in items}
        sealed = [{"questionId": item_id, "answerId": by_item[item_id]["id"], "revision": 1,
                   "answerHash": by_item[item_id]["answer_hash"]}
                  for item_id in sorted(item_ids) if item_id in by_item]
        recovered = len(sealed) == len(items) and all(item["answerHash"] is not None for item in sealed)
        snapshot = {"schemaVersion": "legacy-storage-v1", "session": session, "items": items,
                    "attempts": [row for row in rows["quiz_attempts"] if row["quiz_item_id"] in item_ids],
                    "questions": [row for row in rows["questions"] if row["id"] in question_ids],
                    "sources": [row for row in rows["question_sources"] if row["question_id"] in question_ids],
                    "masteryHistory": [row for row in rows["mastery_history"] if row["quiz_session_id"] == session["id"]]}
        conn.execute(states.insert().values(
            quiz_session_id=session["id"], sealed_at=session["finalized_at"] or session["submitted_at"] or session["created_at"],
            answer_set_hash=_hash([{k: item[k] for k in ("questionId", "revision", "answerHash")} for item in sealed]) if recovered else None,
            input_recovered=recovered, sealed_answers=sealed, legacy=True, legacy_snapshot=_json(snapshot)))


def upgrade():
    conn = op.get_bind()
    legacy = None if context.is_offline_mode() else _legacy(conn)  # preflight before any SQLite DDL
    tables = _create_tables()
    if legacy is not None:
        _backfill(conn, *legacy, *tables)


def downgrade():
    for name in ("quiz_submission_states", "quiz_grading_jobs", "quiz_answer_receipts", "quiz_answer_heads", "quiz_answers"):
        op.drop_table(name)
