"""raw_events dedup and decline category

Revision ID: 381d08b3a2b6
Revises: df641a2a9783
Create Date: 2026-09-04 10:09:59.246926

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '381d08b3a2b6'
down_revision: Union[str, Sequence[str], None] = 'df641a2a9783'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    `provider_event_id` is added NOT NULL with no server default -- safe
    because `raw_events` is new in this same migration chain (df641a2a9783)
    and has never carried a row in any real environment; a table with
    existing data would need a backfill step first.
    """
    op.add_column("raw_events", sa.Column("provider_event_id", sa.String(), nullable=False))
    op.add_column("raw_events", sa.Column("decline_category", sa.String(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_raw_events_provider_event_id"), "raw_events", ["provider_event_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("uq_raw_events_provider_event_id"), "raw_events", type_="unique")
    op.drop_column("raw_events", "decline_category")
    op.drop_column("raw_events", "provider_event_id")
