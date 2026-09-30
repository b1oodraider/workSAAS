"""auto apply: site-agnostic resume title, failure counter, transient attempts

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('autoapply_settings', schema=None) as batch_op:
        batch_op.alter_column('hh_resume_title', new_column_name='site_resume_title',
                              existing_type=sa.String(length=200), existing_nullable=False,
                              server_default='')
        batch_op.add_column(sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False))
    with op.batch_alter_table('applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('applications', schema=None) as batch_op:
        batch_op.drop_column('attempts')
    with op.batch_alter_table('autoapply_settings', schema=None) as batch_op:
        batch_op.drop_column('consecutive_failures')
        batch_op.alter_column('site_resume_title', new_column_name='hh_resume_title',
                              existing_type=sa.String(length=200), existing_nullable=False,
                              server_default=None)
