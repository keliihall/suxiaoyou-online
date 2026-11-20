"""Disposable model-context state, separate from the original transcript."""

from __future__ import annotations

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SessionContextProjection(Base, TimestampMixin):
    __tablename__ = "session_context_projection"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), primary_key=True
    )
    anchor_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("message.id", ondelete="SET NULL"), nullable=True
    )
    boundary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pruned_tool_part_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
