"""add research stage history

Revision ID: 9c31d4e2aa01
Revises: 8d4b2f7c1a11
Create Date: 2026-04-10 10:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "9c31d4e2aa01"
down_revision: Union[str, None] = "8d4b2f7c1a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "research_candidate_stage_history" in tables:
        return

    op.create_table(
        "research_candidate_stage_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=80), nullable=False),
        sa.Column("from_stage", sa.String(length=20), nullable=True),
        sa.Column("to_stage", sa.String(length=20), nullable=False),
        sa.Column("approval_source", sa.String(length=20), nullable=False),
        sa.Column("approved_by", sa.String(length=80), nullable=True),
        sa.Column("approval_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_candidate_stage_history_key_at",
        "research_candidate_stage_history",
        ["candidate_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "research_candidate_stage_history" not in tables:
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("research_candidate_stage_history")}
    if "ix_research_candidate_stage_history_key_at" in indexes:
        op.drop_index("ix_research_candidate_stage_history_key_at", table_name="research_candidate_stage_history")
    op.drop_table("research_candidate_stage_history")
