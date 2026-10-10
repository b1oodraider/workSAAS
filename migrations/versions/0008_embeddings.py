"""meaning-based prefilter: embedding cache, semantic score of a vacancy for a user

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'embedding_cache',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('dims', sa.Integer(), nullable=False),
        sa.Column('vector', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    with op.batch_alter_table('embedding_cache', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_embedding_cache_created_at'), ['created_at'], unique=False)
    with op.batch_alter_table('user_vacancies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('semantic_score', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user_vacancies', schema=None) as batch_op:
        batch_op.drop_column('semantic_score')
    with op.batch_alter_table('embedding_cache', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_embedding_cache_created_at'))
    op.drop_table('embedding_cache')
