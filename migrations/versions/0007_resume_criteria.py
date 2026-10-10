"""resume search criteria (salary floor, countries, formats, grade flexibility)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('resumes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('criteria', sa.JSON(), server_default='{}', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('resumes', schema=None) as batch_op:
        batch_op.drop_column('criteria')
