"""add trade group columns to orders

Revision ID: 5b2f9d9d0f8a
Revises: 0c2363c541cb
Create Date: 2026-04-09 18:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "5b2f9d9d0f8a"
down_revision: Union[str, None] = "0c2363c541cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("orders")}
    indexes = {idx["name"] for idx in inspector.get_indexes("orders")}

    with op.batch_alter_table("orders") as batch_op:
        if "trade_group_id" not in columns:
            batch_op.add_column(sa.Column("trade_group_id", sa.String(length=50), nullable=True))
        if "trade_group_type" not in columns:
            batch_op.add_column(sa.Column("trade_group_type", sa.String(length=30), nullable=True))
        if "ix_orders_trade_group" not in indexes:
            batch_op.create_index(
                "ix_orders_trade_group",
                ["exchange", "trade_group_id", "created_at"],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("orders")}
    indexes = {idx["name"] for idx in inspector.get_indexes("orders")}

    with op.batch_alter_table("orders") as batch_op:
        if "ix_orders_trade_group" in indexes:
            batch_op.drop_index("ix_orders_trade_group")
        if "trade_group_type" in columns:
            batch_op.drop_column("trade_group_type")
        if "trade_group_id" in columns:
            batch_op.drop_column("trade_group_id")
