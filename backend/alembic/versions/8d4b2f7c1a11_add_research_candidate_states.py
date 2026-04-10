"""add research candidate states

Revision ID: 8d4b2f7c1a11
Revises: 5b2f9d9d0f8a
Create Date: 2026-04-10 09:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "8d4b2f7c1a11"
down_revision: Union[str, None] = "5b2f9d9d0f8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "research_candidate_states" in tables:
        return

    op.create_table(
        "research_candidate_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=80), nullable=False),
        sa.Column("approved_stage", sa.String(length=20), nullable=False),
        sa.Column("approval_source", sa.String(length=20), nullable=False),
        sa.Column("approved_by", sa.String(length=80), nullable=True),
        sa.Column("approval_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key", name="uq_research_candidate_state_key"),
    )
    op.create_index(
        "ix_research_candidate_state_stage",
        "research_candidate_states",
        ["approved_stage", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "research_candidate_states" not in tables:
        return

    indexes = {idx["name"] for idx in inspector.get_indexes("research_candidate_states")}
    if "ix_research_candidate_state_stage" in indexes:
        op.drop_index("ix_research_candidate_state_stage", table_name="research_candidate_states")
    op.drop_table("research_candidate_states")
