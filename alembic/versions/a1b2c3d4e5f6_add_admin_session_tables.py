"""add admin session + pending-login tables

Moves the BFF admin sessions and in-flight logins out of in-process dicts into
the DB, so admins stay logged in across restarts and the app can run more than
one worker.

Revision ID: a1b2c3d4e5f6
Revises: 7f52307e0935
Create Date: 2026-07-20 12:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '7f52307e0935'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'admin_sessions',
        sa.Column('sid', sa.String(), primary_key=True),
        sa.Column('access', sa.String(), nullable=False),
        sa.Column('refresh', sa.String(), nullable=True),
        sa.Column('exp', sa.Integer(), nullable=False),
    )
    op.create_table(
        'admin_pending_logins',
        sa.Column('state', sa.String(), primary_key=True),
        sa.Column('verifier', sa.String(), nullable=False),
        sa.Column('created', sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('admin_pending_logins')
    op.drop_table('admin_sessions')
