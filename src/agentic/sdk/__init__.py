"""Claude Agent SDK integration layer.

Phase 3: dev is now an AgentDefinition inside the brain session — there is no
separate dev pool. Public surface narrows to the brain pool, the session store,
the in-process MCP server, and the permission machinery.
"""

from .brain_session import make_brain_options_factory, run_brain_session
from .client_pool import ThreadSessionManager
from .permission import PendingPermissions
from .session_store import SqliteSessionStore

__all__ = [
    "PendingPermissions",
    "SqliteSessionStore",
    "ThreadSessionManager",
    "make_brain_options_factory",
    "run_brain_session",
]
