"""Keep completed demo next-day responses durable across process restarts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_demo_day_requests"
down_revision = "0002_demo_seed"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "demo_day_requests",
        sa.Column("user_id", sa.Uuid(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("response", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "idempotency_key"),
    )


def downgrade():
    op.drop_table("demo_day_requests")
