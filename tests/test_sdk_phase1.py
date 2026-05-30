"""Phase 1+3 hermetic tests for the SDK path.

Asserts:
- PendingPermissions Future create/resolve/timeout semantics
- _needs_confirm wiring (whitelists empty by default in Phase 1)
- Slack permission callback button + timeout flow
- Phase 3 sub-agents: AgentDefinition shape (po/ba/review/dev) +
  brain options wires them into ClaudeAgentOptions.agents
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentic.sdk import PendingPermissions
from agentic.sdk.permission import (
    CONFIRM_BASH_PATTERNS,
    CONFIRM_TOOLS,
    _needs_confirm,
    build_slack_permission_callback,
)
from agentic.sdk.sub_agents import (
    DEV_ALLOWED_TOOLS,
    DEV_DISALLOWED_TOOLS,
    REVIEW_ALLOWED_TOOLS,
    build_subagents,
)
from agentic.store import init_db, touch_thread


async def test_pending_permissions_resolve_round_trip():
    pp = PendingPermissions()
    fut = await pp.create("req-1")
    assert not fut.done()
    assert pp.resolve("req-1", allow=True) is True
    assert fut.result() is True
    # Second resolve is a no-op — Future already consumed/popped.
    assert pp.resolve("req-1", allow=False) is False


async def test_pending_permissions_resolve_unknown():
    pp = PendingPermissions()
    assert pp.resolve("never-existed", allow=True) is False


def test_confirm_tools_gate_destructive_pr_ops():
    """github_merge_pr / github_approve_pr must require confirm — they have
    user-visible side effects and aren't in any allowed_tools list. Matched in
    both bare and `mcp__agentic__`-prefixed form. Bash push stays inline."""
    assert CONFIRM_TOOLS == {"github_merge_pr", "github_approve_pr"}
    assert CONFIRM_BASH_PATTERNS == ()
    assert _needs_confirm("Bash", {"command": "git push origin main"}) is False
    assert _needs_confirm("github_merge_pr", {}) is True
    assert _needs_confirm("mcp__agentic__github_merge_pr", {}) is True
    assert _needs_confirm("mcp__agentic__github_approve_pr", {}) is True
    assert _needs_confirm("mcp__agentic__github_get_pr", {}) is False


async def test_permission_callback_allows_when_not_in_whitelist():
    pp = PendingPermissions()
    slack = AsyncMock()
    cb = build_slack_permission_callback(
        pending=pp, slack_client=slack, channel_id="C1", thread_ts="T1"
    )
    result = await cb("Edit", {"file_path": "/x"}, SimpleNamespace(tool_use_id="t1"))
    assert result.behavior == "allow"
    slack.chat_postMessage.assert_not_called()


async def test_permission_callback_timeout_denies(monkeypatch):
    """When a tool needs confirm and the user never clicks, callback denies."""
    from agentic.sdk import permission as perm_mod

    monkeypatch.setattr(perm_mod, "CONFIRM_TOOLS", {"github_merge_pr"})

    pp = PendingPermissions()
    slack = AsyncMock()
    slack.chat_postMessage = AsyncMock(return_value={"ok": True})
    cb = build_slack_permission_callback(
        pending=pp,
        slack_client=slack,
        channel_id="C1",
        thread_ts="T1",
        timeout_s=0,  # immediate timeout
    )
    result = await cb(
        "github_merge_pr", {"pr": 1}, SimpleNamespace(tool_use_id="t1")
    )
    assert result.behavior == "deny"
    assert "timeout" in result.message
    slack.chat_postMessage.assert_awaited_once()


async def test_permission_callback_allow_via_button(monkeypatch):
    """End-to-end: cb posts buttons → resolve(allow=True) → cb returns Allow."""
    from agentic.sdk import permission as perm_mod

    monkeypatch.setattr(perm_mod, "CONFIRM_TOOLS", {"github_merge_pr"})

    pp = PendingPermissions()
    slack = AsyncMock()
    slack.chat_postMessage = AsyncMock(return_value={"ok": True})
    cb = build_slack_permission_callback(
        pending=pp, slack_client=slack, channel_id="C1", thread_ts="T9"
    )

    # Drive the callback and the resolver concurrently.
    async def resolver():
        # Yield to let cb register the Future first.
        for _ in range(50):
            if pp._futures:
                break
            await asyncio.sleep(0.01)
        req_id = next(iter(pp._futures))
        pp.resolve(req_id, allow=True)

    cb_task = asyncio.create_task(
        cb("github_merge_pr", {"pr": 7}, SimpleNamespace(tool_use_id="t-x"))
    )
    await resolver()
    result = await cb_task
    assert result.behavior == "allow"


# ============================================================================
# Phase 3 — sub-agents
# ============================================================================


def test_build_subagents_keys():
    agents = build_subagents()
    assert set(agents.keys()) == {"po", "ba", "review", "dev"}


def test_build_subagents_po_ba_text_only():
    """po and ba are pure text gen — empty tools list, no MCP server."""
    agents = build_subagents()
    for key in ("po", "ba"):
        ad = agents[key]
        assert ad.tools == []
        # mcpServers default is None when not passed.
        assert ad.mcpServers is None
        # permissionMode None → inherits the brain session's default.
        assert ad.permissionMode is None
        assert ad.prompt  # loaded from prompts/<key>.md
        assert ad.description


def test_build_subagents_review_has_read_and_mcp_pr_tools():
    ad = build_subagents()["review"]
    assert ad.tools == REVIEW_ALLOWED_TOOLS
    # Must include at least one MCP github tool so review can fetch its own diff.
    assert any(t.startswith("mcp__agentic__github_get_pr") for t in ad.tools)
    assert "Read" in ad.tools
    assert ad.mcpServers == ["agentic"]
    assert ad.permissionMode is None  # read-only, no edits


def test_build_subagents_dev_locked_down():
    ad = build_subagents()["dev"]
    assert ad.tools == DEV_ALLOWED_TOOLS
    assert ad.disallowedTools == DEV_DISALLOWED_TOOLS
    assert ad.permissionMode == "acceptEdits"
    # Dev is EDIT-ONLY: the SDK/CLI won't grant a sub-agent Bash (verified
    # 2026-05-30), so dev edits in the worktree and the brain orchestrates
    # git/build. No Bash, and never permission-rule syntax in `tools` (a bare-name
    # allowlist — "Bash(git commit:*)" entries are silently ignored).
    assert "Bash" not in ad.tools
    assert not any("(" in t for t in ad.tools), (
        "AgentDefinition.tools must be bare names, not Bash(...) permission rules"
    )
    # The history-rewrite deny list lives in disallowedTools (rule syntax is valid
    # for deny rules); the brain carries the same list at the session level.
    assert "Bash(git push --force:*)" in ad.disallowedTools
    assert "Bash(git reset --hard:*)" in ad.disallowedTools


async def test_brain_options_factory_wires_subagents():
    """make_brain_options_factory must populate ClaudeAgentOptions.agents with
    the four AgentDefinitions so the brain can delegate via Task."""
    from agentic.sdk.brain_session import make_brain_options_factory

    init_db()
    thread_ts = "1700000000.brain01"
    touch_thread(thread_ts, "C_BRAIN")

    factory = make_brain_options_factory(
        pending=PendingPermissions(),
        session_store=None,
        slack_client=AsyncMock(),
    )
    opts = await factory(thread_ts)
    assert set(opts.agents.keys()) == {"po", "ba", "review", "dev"}
    assert opts.permission_mode == "default"
    assert "agentic" in opts.mcp_servers
    # Brain runs with full default Bash; deny rules (evaluated before can_use_tool)
    # must block history-rewrite at the session level, not only on the dev agent.
    assert "Bash(git push --force:*)" in opts.disallowed_tools
    assert "Bash(git reset --hard:*)" in opts.disallowed_tools
    # Phase 4 — hooks wired for tool logging + compaction (§12.J).
    assert set(opts.hooks) == {
        "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact"
    }
