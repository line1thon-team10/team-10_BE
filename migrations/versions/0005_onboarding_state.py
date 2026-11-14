"""Persist explicit four-field confirmation and draft conversation context.

Legacy goal_confirmed_at is not evidence that optional fields were answered.
Existing goals/messages and plan state stay untouched. Downgrade retains those
rows but discards the new confirmation, context and message ordering metadata.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_onboarding_state"
down_revision = "0004_focus_contract"
branch_labels = None
depends_on = None

CONFIRMATIONS = (
    "onboarding_exam_date_confirmed_at",
    "onboarding_goal_confirmed_at",
    "onboarding_goal_total_confirmed_at",
    "onboarding_goal_etc_confirmed_at",
)


def upgrade():
    for name in CONFIRMATIONS:
        op.add_column("subjects", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    # No other table references onboarding_messages, so SQLite's batch copy
    # preserves all rows and can add the FK without disabling FK checks.
    with op.batch_alter_table("onboarding_messages") as batch:
        batch.add_column(sa.Column("subject_id", sa.Uuid(as_uuid=False),
                                   sa.ForeignKey("subjects.id", name="fk_onboarding_messages_subject_id"),
                                   nullable=True))
        batch.add_column(sa.Column("sort_order", sa.Integer(), nullable=True))
        batch.create_index("ix_onboarding_messages_2", ["user_id", "subject_id", "sort_order"])


def downgrade():
    with op.batch_alter_table("onboarding_messages") as batch:
        batch.drop_index("ix_onboarding_messages_2")
        batch.drop_constraint("fk_onboarding_messages_subject_id", type_="foreignkey")
        batch.drop_column("sort_order")
        batch.drop_column("subject_id")
    for name in reversed(CONFIRMATIONS):
        op.drop_column("subjects", name)
