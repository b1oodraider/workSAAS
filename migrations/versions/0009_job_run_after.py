"""job queue: retry backoff (a queued job waits until run_after)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08 20:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('run_after', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_jobs_run_after'), ['run_after'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_run_after'))
        batch_op.drop_column('run_after')
