"""Select model context without deleting or rewriting transcript records.

Source identifiers are opt-in, internal planning metadata. Normal history
requests never receive them, so they cannot reach a Provider request.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.message import Message
from app.models.session_context_projection import SessionContextProjection

SOURCE_MESSAGE_ID = "_source_message_id"


def select_context_messages(
    messages: list[Message], projection: SessionContextProjection | None,
) -> tuple[list[Message], str | None]:
    """Apply the newest surviving summary/collapse anchor to a read-only view.

    An edited/rewound/deleted anchor cannot hide unrelated history. Legacy
    summaries continue to work without a projection row or a data rewrite.
    """
    summary_anchor = 0
    for index, message in enumerate(messages):
        has_marker = any((part.data or {}).get("type") == "compaction" for part in message.parts)
        has_summary = any(
            (part.data or {}).get("type") == "text"
            and str((part.data or {}).get("text", "")).startswith(("[Context Summary]", "[上下文摘要]"))
            for part in message.parts
        )
        if has_marker and has_summary:
            summary_anchor = index
    if projection is not None and projection.anchor_message_id and projection.boundary_text:
        for index, message in enumerate(messages):
            if message.id == projection.anchor_message_id:
                if index >= summary_anchor and (message.data or {}).get("role") == "user":
                    return messages[index:], projection.boundary_text
                break
    return messages[summary_anchor:], None


def pruned_part_ids(projection: SessionContextProjection | None) -> set[str]:
    values = projection.pruned_tool_part_ids if projection else []
    return {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()


async def get_or_create_projection(db: AsyncSession, session_id: str) -> SessionContextProjection:
    projection = await db.get(SessionContextProjection, session_id)
    if projection is None:
        projection = SessionContextProjection(
            session_id=session_id, boundary_text="", pruned_tool_part_ids=[]
        )
        db.add(projection)
    return projection


async def clear_collapse_anchor(db: AsyncSession, session_id: str) -> None:
    projection = await db.get(SessionContextProjection, session_id)
    if projection is not None:
        projection.anchor_message_id = None
        projection.boundary_text = ""


async def save_context_collapse(
    db: AsyncSession, session_id: str, collapsed_messages: list[dict[str, Any]],
) -> bool:
    """Advance a real user-message boundary; never map LLM counts to DB rows.

    Tool results expand a single Message into several Provider frames, and
    reasoning/accounting-only messages may produce no frames. Only a verified
    source ID can safely identify the retained suffix. Reject a mid-tool-frame
    boundary rather than dropping any part of the current execution.
    """
    if len(collapsed_messages) < 2:
        return False
    boundary_text = collapsed_messages[0].get("content")
    if not isinstance(boundary_text, str) or not boundary_text.strip():
        return False
    retained = collapsed_messages[1:]
    anchor_id = retained[0].get(SOURCE_MESSAGE_ID)
    if not isinstance(anchor_id, str) or retained[0].get("role") != "user":
        return False
    messages = list((await db.scalars(
        select(Message).where(Message.session_id == session_id)
        .options(selectinload(Message.parts))
        .order_by(Message.time_created.asc(), Message.id.asc())
    )).all())
    projection = await db.get(SessionContextProjection, session_id)
    active, _ = select_context_messages(messages, projection)
    positions = {message.id: index for index, message in enumerate(active)}
    anchor_position = positions.get(anchor_id)
    if anchor_position is None or anchor_position == 0:
        return False
    anchor = active[anchor_position]
    if (anchor.data or {}).get("role") != "user":
        return False
    source_positions = [positions.get(frame.get(SOURCE_MESSAGE_ID)) for frame in retained]
    if any(position is None for position in source_positions):
        return False
    if source_positions != sorted(source_positions):
        return False
    projection = await get_or_create_projection(db, session_id)
    projection.anchor_message_id = anchor_id
    projection.boundary_text = boundary_text
    await db.flush()
    return True
