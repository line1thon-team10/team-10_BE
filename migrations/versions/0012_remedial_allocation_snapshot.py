"""Retain exact pace inputs of remedial placements without guessing legacy data."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0012_remedial_allocation'
down_revision = '0011_contract_invariants'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('remedial_obligation_blocks',sa.Column('allocation_snapshot',sa.JSON().with_variant(JSONB(),'postgresql'),nullable=True))


def downgrade():
    op.drop_column('remedial_obligation_blocks','allocation_snapshot')
