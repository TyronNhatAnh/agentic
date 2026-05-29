"""SQLite-backed SessionStore adapter for claude-agent-sdk.

Mirrors session transcripts into the `threads` table (column `sdk_state_blob`)
keyed by `sdk_session_id`. Subprocess still writes its own JSONL locally; this
adapter is the durable copy the bot resumes from across restarts.

Phase 0: skeleton with required `append` / `load` only. Subagent transcripts,
list/list_subkeys, delete, and summary methods raise NotImplementedError —
SDK probes for them at runtime and falls back gracefully (see types.py:1390).
Phase 1 fills in what dev/brain sessions actually need.
"""

from __future__ import annotations

import json
import time
from typing import Any

from claude_agent_sdk import SessionKey, SessionStoreEntry

from ..store import connect


class SqliteSessionStore:
    """Persist SDK session transcripts in the `threads` table.

    One row per Slack thread. `sdk_session_id` holds the latest session UUID;
    `sdk_state_blob` holds the full transcript as JSON-encoded list[entry]. For
    Phase 0 we overwrite the blob on each append — Phase 1 will switch to
    incremental append + size cap to avoid rewriting megabytes per turn.
    """

    async def append(
        self, key: SessionKey, entries: list[SessionStoreEntry]
    ) -> None:
        if key.get("subpath"):
            return  # subagent transcripts: not persisted in Phase 0
        thread_ts = key["project_key"]
        session_id = key["session_id"]
        existing = await self._load_blob(thread_ts, session_id)
        existing.extend(entries)
        blob = json.dumps(existing)
        with connect() as conn:
            conn.execute(
                "UPDATE threads SET sdk_session_id=?, sdk_state_blob=? "
                "WHERE thread_ts=?",
                (session_id, blob, thread_ts),
            )

    async def load(
        self, key: SessionKey
    ) -> list[SessionStoreEntry] | None:
        if key.get("subpath"):
            return None
        thread_ts = key["project_key"]
        session_id = key["session_id"]
        return await self._load_blob(thread_ts, session_id) or None

    async def _load_blob(
        self, thread_ts: str, session_id: str
    ) -> list[SessionStoreEntry]:
        with connect() as conn:
            row = conn.execute(
                "SELECT sdk_session_id, sdk_state_blob FROM threads "
                "WHERE thread_ts=?",
                (thread_ts,),
            ).fetchone()
        if not row or row["sdk_session_id"] != session_id:
            return []
        raw = row["sdk_state_blob"] or "[]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    # Phase 1+ — implement when brain/dev sessions need them.
    # The SDK probes for these at runtime; omitting them just disables the
    # corresponding optional features (list/resume-by-mtime, cleanup, summaries).
    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        raise NotImplementedError("Phase 1")

    async def delete(self, key: SessionKey) -> None:
        raise NotImplementedError("Phase 1")
