"""
ARIA - Conversation message archive

Phase: Performance review (D17)
Purpose: keep `conversations.messages` bounded without losing history.

Every turn `$push`es onto an array that nothing ever trimmed, and each push
rewrites the whole document -- so a long conversation gets progressively more
expensive per turn, forever.

Overflow moves to `conversation_archives` rather than being dropped: dropping
is a one-way door, and the transcript is the record of the work. The archive
is written BEFORE the inline array is trimmed, so a crash between the two
duplicates a message rather than losing one.

Read path: `messages_page()` serves a window across the seam, so the API's
backward pagination is unchanged by where a message physically lives.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)


async def archive_overflow(db, conversation_id: Any, cap: int) -> int:
    """Move all but the newest `cap` messages into the archive.

    Returns how many messages were archived. Safe to call on every turn: it
    is a no-op (one projected read) while the conversation is under the cap.
    """
    if cap <= 0:
        return 0
    oid = conversation_id if isinstance(conversation_id, ObjectId) else ObjectId(str(conversation_id))

    doc = await db.conversations.find_one({"_id": oid}, {"messages": 1})
    if not doc:
        return 0
    messages = doc.get("messages") or []
    overflow = len(messages) - cap
    if overflow <= 0:
        return 0

    moving = messages[:overflow]
    # Only messages carrying an id can be pulled back out precisely. Anything
    # without one stays inline rather than risking a $pull that matches more
    # than intended.
    ids = [m.get("id") for m in moving if isinstance(m, dict) and m.get("id")]
    if not ids:
        logger.warning(
            "conversation %s: %d overflow messages have no id; leaving them inline",
            oid, overflow,
        )
        return 0
    moving = [m for m in moving if m.get("id") in set(ids)]

    # Archive FIRST. A crash here leaves the conversation untrimmed, which is
    # the harmless direction.
    await db.conversation_archives.insert_one({
        "conversation_id": oid,
        "messages": moving,
        "archived_at": datetime.now(timezone.utc),
        "first_id": moving[0].get("id"),
        "last_id": moving[-1].get("id"),
        "count": len(moving),
    })

    # $pull by exact id, not a $slice trim: new messages may have arrived
    # since the read above, and a slice would drop more than was archived.
    await db.conversations.update_one(
        {"_id": oid},
        {
            "$pull": {"messages": {"id": {"$in": ids}}},
            # A counter on the doc so the read path knows whether an archive
            # exists WITHOUT querying it -- otherwise every short conversation
            # (window wider than the inline array) pays an archive lookup that
            # can only ever come back empty.
            "$inc": {"archived_message_count": len(moving)},
        },
    )
    logger.info("conversation %s: archived %d messages", oid, len(moving))
    return len(moving)


async def archived_count(db, conversation_id: Any) -> int:
    """How many messages live in the archive for this conversation."""
    oid = conversation_id if isinstance(conversation_id, ObjectId) else ObjectId(str(conversation_id))
    total = 0
    async for d in db.conversation_archives.find(
        {"conversation_id": oid}, {"count": 1}
    ):
        total += int(d.get("count") or 0)
    return total


async def messages_page(
    db, conversation_id: Any, *, msg_limit: int, msg_skip: int
) -> Optional[list[dict]]:
    """A backward-paginated window of messages, reading across the seam.

    Indexing matches the inline `$slice` semantics the API already exposes:
    `msg_skip` counts back from the most recent message. Returns None when
    the conversation does not exist.
    """
    oid = conversation_id if isinstance(conversation_id, ObjectId) else ObjectId(str(conversation_id))
    doc = await db.conversations.find_one({"_id": oid}, {"messages": 1})
    if not doc:
        return None
    inline = doc.get("messages") or []

    # Fast path: either nothing was ever archived, or the window lies entirely
    # within the inline tail. Both cost no archive read at all.
    if not int(doc.get("archived_message_count") or 0) or msg_skip + msg_limit <= len(inline):
        if msg_limit <= 0:
            return []
        end = max(0, len(inline) - msg_skip)
        start = max(0, end - msg_limit)
        return inline[start:end]

    archives = await db.conversation_archives.find(
        {"conversation_id": oid}
    ).sort("archived_at", 1).to_list(length=1000)
    older: list[dict] = []
    for a in archives:
        older.extend(a.get("messages") or [])

    full = older + inline
    end = len(full) - msg_skip
    start = max(0, end - msg_limit)
    if end <= 0:
        return []
    return full[start:end]
