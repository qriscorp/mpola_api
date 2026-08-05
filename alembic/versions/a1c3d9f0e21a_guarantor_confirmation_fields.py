"""guarantor confirmation fields

Revision ID: a1c3d9f0e21a
Revises: ba52e5a429d6
Create Date: 2026-08-05 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = 'a1c3d9f0e21a'
down_revision: Union[str, None] = 'ba52e5a429d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('guarantors', sa.Column('confirmation_token', sa.String(length=64), nullable=True))
    op.add_column('guarantors', sa.Column('responded_at', sa.DateTime(), nullable=True))
    op.create_unique_constraint('uq_guarantors_confirmation_token', 'guarantors', ['confirmation_token'])


def downgrade() -> None:
    op.drop_constraint('uq_guarantors_confirmation_token', 'guarantors', type_='unique')
    op.drop_column('guarantors', 'responded_at')
    op.drop_column('guarantors', 'confirmation_token')
