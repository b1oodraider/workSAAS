"""match feedback (👍/👎 with reason) and employer response quality

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user_vacancies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('match_vote', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('match_vote_reason', sa.String(length=16), server_default='', nullable=False))
        batch_op.add_column(sa.Column('response_quality', sa.String(length=16), server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('user_vacancies', schema=None) as batch_op:
        batch_op.drop_column('response_quality')
        batch_op.drop_column('match_vote_reason')
        batch_op.drop_column('match_vote')
