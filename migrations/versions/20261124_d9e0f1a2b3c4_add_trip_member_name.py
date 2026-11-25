"""add trip_members.name

The name an organizer gives someone on the roster, for showing them before
(or without) an account. A linked account's own display name wins on read; this
one is kept underneath so the person still has a name if the account goes.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-11-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd9e0f1a2b3c4'
down_revision: Union[str, None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trip_members', sa.Column('name', sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column('trip_members', 'name')
