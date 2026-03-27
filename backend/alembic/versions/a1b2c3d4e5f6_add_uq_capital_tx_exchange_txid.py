"""add uq_capital_tx_exchange_txid partial index

Revision ID: a1b2c3d4e5f6
Revises: b033178c02bc
Create Date: 2026-03-27 00:00:00.000000

Adds a partial unique index on capital_transactions(exchange, exchange_tx_id)
WHERE exchange_tx_id IS NOT NULL to prevent duplicate auto-detected transfer
records when concurrent scheduler ticks race to insert the same tranId.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'b033178c02bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_tx_exchange_txid "
        "ON capital_transactions (exchange, exchange_tx_id) "
        "WHERE exchange_tx_id IS NOT NULL"
    ))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS uq_capital_tx_exchange_txid"))
