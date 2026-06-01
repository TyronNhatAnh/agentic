"""SQLite-backed SessionStore adapter for claude-agent-sdk.

Mirrors session transcripts into the `session_entries` table keyed by
(thread_ts, sdk_session_id). The subprocess still writes its own JSONL locally;
this adapter is the durable copy the bot resumes from across restarts.

Append-only: each `append` inserts the new entries in a single transaction
(O(new entries), atomic — a crash rolls back instead of leaving a half-written
blob). This replaced the Phase 0 read-modify-write of `threads.sdk_state_blob`,
which rewrote the whole transcript every turn and could corrupt on a mid-write
crash. `load` falls back to the legacy blob once so sessions persisted before
the migration still resume.

Subagent transcripts (keys with a `subpath`) are still not persisted — they are
recreated from the brain's delegation each turn. list/delete remain Phase 1.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import SessionKey, SessionStoreEntry

from ..store import append_session_entries, load_session_entries, run_db


class SqliteSessionStore:
    """Persist SDK session transcripts append-only in `session_entries`."""

    async def append(
        self, key: SessionKey, entries: list[SessionStoreEntry]
    ) -> None:
        if key.get("subpath"):
            return  # subagent transcripts: not persisted
        if not entries:
            return
        serialized = [json.dumps(e) for e in entries]
        await run_db(
            append_session_entries,
            key["project_key"],
            key["session_id"],
            serialized,
        )

    async def load(
        self, key: SessionKey
    ) -> list[SessionStoreEntry] | None:
        if key.get("subpath"):
            return None
        rows = await run_db(
            load_session_entries, key["project_key"], key["session_id"]
        )
        if not rows:
            return None
        return [json.loads(r) for r in rows]

    # Phase 1+ — implement when brain/dev sessions need them. The SDK probes for
    # these at runtime; omitting them just disables the corresponding optional
    # features (list/resume-by-mtime, cleanup).
    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        raise NotImplementedError("Phase 1")

    async def delete(self, key: SessionKey) -> None:
        raise NotImplementedError("Phase 1")
