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

Storage key gotcha: `SessionKey.project_key` is derived by the SDK from `cwd`, so
every Slack thread pointed at the same repo shares one project_key. Using it as
the row key made thread B's append prune thread A's transcript (the DELETE in
`append_session_entries`) and left `threads.sdk_session_id` unwritten (no row has
a repo path as its PK). So the store is *bound to a thread* by the options
factory via `for_thread()`; the unbound instance keeps the old key for tests.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import SessionKey, SessionStoreEntry

from ..store import append_session_entries, load_session_entries, run_db


class SqliteSessionStore:
    """Persist SDK session transcripts append-only in `session_entries`."""

    def __init__(self, thread_ts: str | None = None) -> None:
        self._thread_ts = thread_ts

    def for_thread(self, thread_ts: str) -> SqliteSessionStore:
        """Bind a copy to one Slack thread so rows key on `thread_ts`."""
        return SqliteSessionStore(thread_ts)

    def _row_key(self, key: SessionKey) -> str:
        return self._thread_ts or key["project_key"]

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
            self._row_key(key),
            key["session_id"],
            serialized,
        )

    async def load(
        self, key: SessionKey
    ) -> list[SessionStoreEntry] | None:
        if key.get("subpath"):
            return None
        rows = await run_db(
            load_session_entries, self._row_key(key), key["session_id"]
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
