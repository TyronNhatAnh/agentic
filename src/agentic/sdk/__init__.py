"""Claude Agent SDK integration layer.

This package wraps `claude-agent-sdk` for use inside the bot. Phase 0 ships
skeleton modules; Phase 1+ fills them in. See MIGRATION_PLAN.md.

Public surface (stable across phases):
- ThreadSessionManager: pool of ClaudeSDKClient per Slack thread_ts.
- SqliteSessionStore:   SessionStore adapter persisting into the `threads` table.
- agentic_mcp_server:   in-process MCP server exposing integration verbs.
"""

from .brain_session import make_brain_options_factory, run_brain_session
from .client_pool import ThreadSessionManager
from .dev_agent import make_dev_options_factory, run_dev_sdk
from .permission import PendingPermissions
from .session_store import SqliteSessionStore

__all__ = [
    "PendingPermissions",
    "SqliteSessionStore",
    "ThreadSessionManager",
    "make_brain_options_factory",
    "make_dev_options_factory",
    "run_brain_session",
    "run_dev_sdk",
]
