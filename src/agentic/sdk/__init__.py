"""Claude Agent SDK integration layer.

This package wraps `claude-agent-sdk` for use inside the bot. Phase 0 ships
skeleton modules; Phase 1+ fills them in. See MIGRATION_PLAN.md.

Public surface (stable across phases):
- ThreadSessionManager: pool of ClaudeSDKClient per Slack thread_ts.
- SqliteSessionStore:   SessionStore adapter persisting into the `threads` table.
- agentic_mcp_server:   in-process MCP server exposing integration verbs.
"""

from .client_pool import ThreadSessionManager
from .session_store import SqliteSessionStore

__all__ = [
    "ThreadSessionManager",
    "SqliteSessionStore",
]
