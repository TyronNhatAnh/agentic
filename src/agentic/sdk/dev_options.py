"""ClaudeAgentOptions builder for the dev sub-agent (Phase 1 — §12.C).

Mirrors the allow / deny lists from the legacy agents/dev.py so behavior is
1-to-1 except: always `acceptEdits` (root-cause fix for the dev_cwd=None
sandbox bug — Phase 1 decision in §8), and uses the SDK session for resume +
streaming.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from ..agents.base import load_prompt
from ..config import settings
from ..store import get_thread

# Mirrors agents/dev.py:_DEV_ALLOWED_TOOLS plus the editor/search primitives
# the SDK exposes natively (Read/Write/Edit/Glob/Grep) — legacy `claude -p`
# inherited those via host config, the SDK wants them explicit.
DEV_ALLOWED_TOOLS: list[str] = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git fetch:*)",
    "Bash(git rev-parse:*)",
    "Bash(git branch:*)",
    "Bash(git checkout:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr list:*)",
    "Bash(gh pr comment:*)",
]

# Copied verbatim from agents/dev.py — never rewrite history, never force-push.
DEV_DISALLOWED_TOOLS: list[str] = [
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git push --force-with-lease:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean -fd:*)",
    "Bash(git clean -f:*)",
    "Bash(git branch -D:*)",
]


def _resume_session_id(thread_ts: str) -> str | None:
    """Return the prior SDK session_id for this thread if one is persisted.

    Cheap one-row lookup; SqliteSessionStore materializes the transcript when
    the SDK calls `load()` during resume. Phase 1 includes this so a process
    restart doesn't drop conversation state mid-thread.
    """
    row = get_thread(thread_ts)
    if not row:
        return None
    sid = (row.get("sdk_session_id") or "").strip()
    return sid or None


def build_dev_options(
    *,
    thread_ts: str,
    cwd: str | None,
    permission_cb: Any,
    session_store: Any,
) -> ClaudeAgentOptions:
    """Compose ClaudeAgentOptions for a dev session in a single Slack thread.

    cwd: worktree path when one is open for the thread, otherwise None — falls
    back to `settings.workspace_dir` so the agent can still browse repos when
    no specific service is pinned.
    """
    system_prompt = load_prompt("dev")
    effective_cwd = cwd or (settings.workspace_dir or None)

    # Match legacy run_claude behavior (agents/base.py:67-72): when the cwd is
    # a real worktree under workspace_dir, expose the workspace root so the
    # agent can read sibling repos. No need to add cwd itself — it's already
    # the working directory.
    add_dirs: list[str] = []
    if (
        effective_cwd
        and settings.workspace_dir
        and effective_cwd != settings.workspace_dir
    ):
        add_dirs.append(settings.workspace_dir)

    resume = _resume_session_id(thread_ts)

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="acceptEdits",
        allowed_tools=DEV_ALLOWED_TOOLS,
        disallowed_tools=DEV_DISALLOWED_TOOLS,
        add_dirs=add_dirs,
        cwd=effective_cwd,
        can_use_tool=permission_cb,
        session_store=session_store,
        model=settings.dev_model,
        include_partial_messages=True,
        resume=resume,
    )
