"""scheduled_actions executed_at

Revision ID: c046a81958ee
Revises: 381d08b3a2b6
Create Date: 2026-09-04 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c046a81958ee'
down_revision: Union[str, Sequence[str], None] = '381d08b3a2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Nullable, no backfill: existing `done` rows (there are none in any
    real environment yet -- the executor that sets this shipped in the
    same phase) simply have no recorded execution time, which is honest
    given none was captured for them.
    """
    op.add_column("scheduled_actions", sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("scheduled_actions", "executed_at")
