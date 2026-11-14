"""Preserve preview bases, state revisions and first responses.

Online upgrade copies known legacy facts only. SQL-only rendering emits DDL;
legacy candidates have no recoverable initial response or version fingerprint.
Downgrade removes the new sidecar, retaining all pre-existing history.
"""
from datetime import date, datetime
import json

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_preview_storage"
down_revision = "0006_quiz_answer_storage"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(JSONB(), "postgresql")
UUID = sa.Uuid(as_uuid=False)

CHECKS = [
    ("ck_proposal_state_status", "status IN ('ready', 'applied', 'cancelled', 'stale', 'legacy') AND state_revision >= 0"),
    ("ck_proposal_state_hash", "(input_hash IS NULL OR length(input_hash) = 64) AND (base_structure_hash IS NULL OR length(base_structure_hash) = 64)"),
    ("ck_proposal_state_origin", "(legacy = true AND legacy_snapshot IS NOT NULL AND status IN ('legacy', 'applied')) OR (legacy = false AND legacy_snapshot IS NULL AND status <> 'legacy')"),
    ("ck_proposal_state_basis", "legacy = true OR (target IS NOT NULL AND normalized_input IS NOT NULL AND input_hash IS NOT NULL AND base_date IS NOT NULL AND dependency_versions IS NOT NULL AND preview_response IS NOT NULL AND preview_schema_version IS NOT NULL AND preview_schema_version > 0 AND ((base_plan_id IS NOT NULL AND base_plan_revision IS NOT NULL AND base_plan_revision > 0 AND base_structure_hash IS NULL) OR (base_plan_id IS NULL AND base_plan_revision IS NULL AND base_structure_hash IS NOT NULL)))"),
    ("ck_proposal_state_result", "(result_response IS NULL AND result_schema_version IS NULL AND result_captured_at IS NULL AND (status <> 'applied' OR legacy = true)) OR (legacy = false AND status = 'applied' AND result_response IS NOT NULL AND result_schema_version IS NOT NULL AND result_schema_version > 0 AND result_captured_at IS NOT NULL AND decided_at IS NOT NULL AND result_captured_at = decided_at)"),
    ("ck_proposal_state_decision", "legacy = true OR (status = 'ready' AND decided_at IS NULL) OR (status IN ('applied', 'cancelled', 'stale') AND decided_at IS NOT NULL)"),
]


def _legacy_rows(conn):
    metadata = sa.MetaData()
    requests = sa.Table("plan_change_requests", metadata, autoload_with=conn)
    proposals = sa.Table("plan_proposals", metadata, autoload_with=conn)
    for table in (requests, proposals):
        for column in table.c:
            if column.name == "id" or column.name.endswith("_id"):
                column.type = UUID
    request_rows = {row["id"]: dict(row) for row in conn.execute(sa.select(requests)).mappings()}
    return [(dict(row), request_rows[row["request_id"]]) for row in conn.execute(sa.select(proposals)).mappings()]


def _snapshot(value):
    def convert(item):
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        raise TypeError(f"Unsupported legacy value: {type(item).__name__}")
    return json.loads(json.dumps(value, default=convert, ensure_ascii=False, allow_nan=False))


def upgrade():
    conn = op.get_bind()
    legacy = [] if context.is_offline_mode() else _legacy_rows(conn)
    states = op.create_table(
        "plan_proposal_states",
        sa.Column("proposal_id", UUID, sa.ForeignKey("plan_proposals.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target", JSON, nullable=True),
        sa.Column("normalized_input", JSON, nullable=True),
        sa.Column("input_hash", sa.String(), nullable=True),
        sa.Column("base_date", sa.Date(), nullable=True),
        sa.Column("base_plan_id", UUID, sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("base_plan_revision", sa.Integer(), nullable=True),
        sa.Column("base_structure_hash", sa.String(), nullable=True),
        sa.Column("dependency_versions", JSON, nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("preview_response", JSON, nullable=True),
        sa.Column("preview_schema_version", sa.Integer(), nullable=True),
        sa.Column("result_response", JSON, nullable=True),
        sa.Column("result_schema_version", sa.Integer(), nullable=True),
        sa.Column("result_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legacy", sa.Boolean(), nullable=False),
        sa.Column("legacy_snapshot", JSON, nullable=True),
        *(sa.CheckConstraint(sql, name=name) for name, sql in CHECKS),
    )
    op.create_index("ix_plan_proposal_states_1", "plan_proposal_states", ["user_id", "kind", "status"])
    for proposal, request in legacy:
        applied = request["status"] == "accepted" and request["accepted_proposal_id"] == proposal["id"]
        conn.execute(states.insert().values(
            proposal_id=proposal["id"], user_id=request["user_id"], kind=request["kind"],
            target=sa.null(), normalized_input=sa.null(), input_hash=None, base_date=None,
            base_plan_id=request["base_plan_id"], base_plan_revision=request["base_plan_revision"],
            base_structure_hash=None, dependency_versions=sa.null(), status="applied" if applied else "legacy",
            state_revision=0, preview_response=sa.null(), preview_schema_version=None,
            result_response=sa.null(), result_schema_version=None, result_captured_at=None,
            decided_at=request["decided_at"] if applied else None, legacy=True,
            legacy_snapshot=_snapshot({"request": request, "proposal": proposal}),
        ))


def downgrade():
    op.drop_index("ix_plan_proposal_states_1", table_name="plan_proposal_states")
    op.drop_table("plan_proposal_states")
