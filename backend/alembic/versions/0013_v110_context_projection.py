"""Preserve transcript content while persisting disposable model-context state.

Revision ID: 0013_v110_context_projection
Revises: 0012_v110_workspace_identity_v2
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_v110_context_projection"
down_revision = "0012_v110_workspace_identity_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_context_projection",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("anchor_message_id", sa.String(), nullable=True),
        sa.Column("boundary_text", sa.Text(), nullable=False),
        sa.Column("pruned_tool_part_ids", sa.JSON(), nullable=False),
        sa.Column("time_created", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("time_updated", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["anchor_message_id"], ["message.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("session_id"),
    )


def downgrade() -> None:
    # This table contains only rebuildable LLM context, never original content.
    op.drop_table("session_context_projection")
