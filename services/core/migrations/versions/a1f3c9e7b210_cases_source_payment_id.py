"""cases source_payment_id

Revision ID: a1f3c9e7b210
Revises: c046a81958ee
Create Date: 2026-09-05 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f3c9e7b210'
down_revision: Union[str, Sequence[str], None] = 'c046a81958ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Nullable, no backfill: existing `done` rows (none in any real
    environment yet) simply have no recorded source payment, honest given
    none was captured for them. `None` is also the correct, permanent value
    for an L3 (halted subscription) case -- there is no one payment
    attempt underneath a subscription halt to retry (T3.5).
    """
    op.add_column("cases", sa.Column("source_payment_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("cases", "source_payment_id")
