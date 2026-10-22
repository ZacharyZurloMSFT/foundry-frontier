"""Owner-bound conversation history: a session belongs to the actor that created it, and nobody else may read it.

An exchange is stored as one document, so a write either saves the question with its answer or saves nothing.
Only `assistant_exchange` documents are ever read back: anything else a writer puts in a session's partition is
ignored, so it can neither become model context nor break the owner's conversation.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..business.models import BusinessError
from ..repositories import _container
from ..settings import get_settings

SESSION_PREFIX = "asst-"
HISTORY_EXCHANGES = 6
CONTENT_LIMIT = 3000
SESSION_EXCHANGE_LIMIT = 100
RECORD_KIND = "assistant_exchange"
_last_stamp = datetime.min.replace(tzinfo=timezone.utc)


def _stamp() -> str:
    """A strictly increasing timestamp, so exchanges keep their order even when the clock has not ticked."""
    global _last_stamp
    now = datetime.now(timezone.utc)
    _last_stamp = now if now > _last_stamp else _last_stamp + timedelta(microseconds=1)
    return _last_stamp.isoformat()


def new_session_id(surface: str) -> str:
    return f"{SESSION_PREFIX}{surface}-{uuid.uuid4()}"


def session_surface(session_id: str) -> str:
    return session_id.split("-", 2)[1]


def session_ref(session_id: str) -> str:
    """The only form of a session id that may reach telemetry: a one-way reference, never the id itself."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


async def load_history(session_id: str, actor_key: str) -> list[dict[str, str]]:
    """Latest bounded exchanges of this actor's own session; any foreign or oversized session is refused."""
    container = await _container("conversations")
    rows: list[dict[str, Any]] = [
        row async for row in container.query_items(
            query=(f"SELECT TOP {SESSION_EXCHANGE_LIMIT + 1} c.ownerKey, c.question, c.answer, c.ts FROM c "
                   "WHERE c.sessionId = @sid AND c.record_kind = @kind ORDER BY c.ts DESC"),
            parameters=[{"name": "@sid", "value": session_id}, {"name": "@kind", "value": RECORD_KIND}],
            partition_key=session_id,
        )
    ]
    if any(row.get("ownerKey") != actor_key for row in rows):
        raise BusinessError("session_not_found", "This conversation was not found.", 404)
    if len(rows) > SESSION_EXCHANGE_LIMIT:
        raise BusinessError("session_full", "This conversation is full. Start a new one.", 409)
    messages: list[dict[str, str]] = []
    for row in reversed(rows[:HISTORY_EXCHANGES]):
        if isinstance(row.get("question"), str) and isinstance(row.get("answer"), str):
            messages.append({"role": "user", "content": row["question"][:CONTENT_LIMIT]})
            messages.append({"role": "assistant", "content": row["answer"][:CONTENT_LIMIT]})
    return messages


async def append_turn(session_id: str, actor_key: str, surface: str, question: str, answer: str,
                      meta: dict[str, Any]) -> None:
    """Persist a successful exchange only, so blocked or failed attempts never become model context."""
    container = await _container("conversations")
    await container.create_item({
        "id": str(uuid.uuid4()), "sessionId": session_id, "record_kind": RECORD_KIND,
        "ownerKey": actor_key, "surface": surface, "question": question[:CONTENT_LIMIT],
        "answer": answer[:CONTENT_LIMIT], "meta": meta, "ts": _stamp(),
        "ttl": get_settings().assistant_history_ttl_seconds,
    })
