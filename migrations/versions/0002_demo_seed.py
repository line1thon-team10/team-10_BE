"""Persist the selected demo seed without guessing legacy choices."""
from alembic import op
import sqlalchemy as sa

revision = "0002_demo_seed"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("demo_seed", sa.String(), nullable=True))


def downgrade():
    op.drop_column("users", "demo_seed")
