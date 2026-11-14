"""Persist grading execution policy across process restarts and expired leases.

Existing jobs retain their answers/results and receive the previous live policy.
The database default preserves inserts from code that omits the new column.
Downgrade loses saved/backup intent: stop workers and back up before rollback.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_grading_execution_mode"
down_revision = "0007_preview_storage"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("quiz_grading_jobs") as batch:
        batch.add_column(sa.Column("execution_mode", sa.String(), nullable=False, server_default="live"))
        batch.create_check_constraint("ck_quiz_job_execution_mode", "execution_mode IN ('live', 'saved', 'backup')")


def downgrade():
    with op.batch_alter_table("quiz_grading_jobs") as batch:
        batch.drop_constraint("ck_quiz_job_execution_mode", type_="check")
        batch.drop_column("execution_mode")
