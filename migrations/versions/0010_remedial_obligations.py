"""Keep immutable remedial obligations even when no legal placement exists."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0010_remedial_obligations'
down_revision = '0009_record_requests'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('remedial_obligations',
        sa.Column('id',sa.Uuid(as_uuid=False),primary_key=True),
        sa.Column('user_id',sa.Uuid(as_uuid=False),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('unit_id',sa.Uuid(as_uuid=False),sa.ForeignKey('units.id'),nullable=False),
        sa.Column('quiz_session_id',sa.Uuid(as_uuid=False),sa.ForeignKey('quiz_sessions.id'),nullable=False),
        sa.Column('scope_revision',sa.String(),nullable=False),
        sa.Column('required_ranges',sa.JSON().with_variant(JSONB(),'postgresql'),nullable=False),
        sa.Column('not_before',sa.Date(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_remedial_obligations_1','remedial_obligations',['unit_id','quiz_session_id'],unique=True)
    op.create_table('remedial_obligation_blocks',
        sa.Column('obligation_id',sa.Uuid(as_uuid=False),sa.ForeignKey('remedial_obligations.id'),nullable=False),
        sa.Column('block_id',sa.Uuid(as_uuid=False),sa.ForeignKey('study_blocks.id'),nullable=False,unique=True),
        sa.PrimaryKeyConstraint('obligation_id','block_id'))


def downgrade():
    op.drop_table('remedial_obligation_blocks')
    op.drop_table('remedial_obligations')
