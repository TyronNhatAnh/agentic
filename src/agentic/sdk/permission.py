"""Permission flow for SDK sessions (Phase 1 — §12.A).

PendingPermissions holds in-process Future objects, one per outstanding tool
permission request. The Slack button handlers in `slack_handlers.py` resolve
those Futures via `pending.resolve(req_id, allow)`. The session task is blocked
inside an `await asyncio.wait_for(future, timeout)` within the `can_use_tool`
callback the SDK invokes — no DB persistence needed because the SDK keeps the
request open across the same async context.

Phase 1 ships the machinery with empty whitelists. Why empty: the SDK skips
`can_use_tool` for any tool already in `allowed_tools` / `permission_mode`
([SDK types.py:1748-1758](file:///tmp/casdk/unpacked/claude_agent_sdk/types.py#L1748)),
and the dev agent allow-lists `Bash(git push:*)` etc. for end-to-end flow.
Phase 2 populates CONFIRM_TOOLS with `github_merge_pr` — it isn't in
`allowed_tools` so the callback actually fires. If we ever need to gate an already-allowed tool,
use a `PreToolUse` hook instead (Phase 4).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool gating lives in ONE module so the three layers can't drift apart. When
# adding a tool to mcp_tools.py, decide which layer (if any) it needs here:
#
#   1. SESSION_DISALLOWED_TOOLS — structural hard deny. The SDK strips these from
#      context *before* can_use_tool runs, so it's a block, not a prompt request.
#      Applied to the brain session AND every sub-agent (history-rewrite git).
#   2. tool_scope (built per channel in policy.py) — an allowlist enforced by the
#      callback below; a tool outside it is denied. Unused today (prod = no gate),
#      kept as the extension point for a future clamped channel.
#   3. CONFIRM_TOOLS — human-in-the-loop: the callback posts a Slack button and
#      blocks until the user allows/denies (side-effecting PR ops).
# ---------------------------------------------------------------------------

# Session-wide hard deny — never rewrite history or force-push, for the brain or
# any sub-agent. Single source of truth; sub_agents.py re-exports it as
# DEV_DISALLOWED_TOOLS and brain_session passes it to disallowed_tools.
SESSION_DISALLOWED_TOOLS: list[str] = [
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git push --force-with-lease:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean -fd:*)",
    "Bash(git clean -f:*)",
    "Bash(git branch -D:*)",
]

# Tools that always require user confirm via Slack button — those with
# user-visible side effects. None are in any `allowed_tools` list, so the SDK
# actually routes them through this callback. `github_merge_pr` keeps its
# mergeable_state guard but still must not merge without a human in the loop.
# Stored as bare names — `_needs_confirm` strips the `mcp__<server>__` prefix the
# control protocol uses so either form matches.
#
# `github_approve_pr` is deliberately NOT here: the review flow auto-approves
# (LGTM) when the review sub-agent's verdict is APPROVE, and a button on every
# clean PR defeats that. An approval is reversible (dismiss/re-request) and merge
# is still gated, so the human stays in the loop where it matters.
#
# `db_query_prod` is deliberately NOT here: a prod-read turn fans out to many
# queries, so one button per call made it unusable. Writes are already impossible
# (guard_sql allows one read-only statement; the replica is @@read_only=1), and
# every call stays audit-logged server-side — the gate only bought a human
# green-light on reading PII, which the operator chose to drop.
CONFIRM_TOOLS: set[str] = {"github_merge_pr"}

# Bash commands whose `command` field, when prefix-matched, triggers confirm.
# Phase 1: empty (push allowed inline). Phase 2 may revisit.
CONFIRM_BASH_PATTERNS: tuple[str, ...] = ()


def _needs_confirm(tool_name: str, tool_input: dict[str, Any] | None) -> bool:
    # MCP tools arrive as `mcp__agentic__github_merge_pr`; bare builtins as `Bash`.
    bare = tool_name.rsplit("__", 1)[-1]
    if tool_name in CONFIRM_TOOLS or bare in CONFIRM_TOOLS:
        return True
    if tool_name == "Bash":
        cmd = (tool_input or {}).get("command", "") or ""
        return any(cmd.startswith(p) for p in CONFIRM_BASH_PATTERNS)
    return False


class PendingPermissions:
    """In-memory map: req_id → Future[bool]. Per-process, no DB."""

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()

    async def create(self, req_id: str) -> asyncio.Future[bool]:
        async with self._lock:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bool] = loop.create_future()
            self._futures[req_id] = fut
            return fut

    def resolve(self, req_id: str, allow: bool) -> bool:
        """Resolve a pending Future. Returns True if req_id was pending; False
        otherwise (caller can treat as "no-op, message is unrelated")."""
        fut = self._futures.pop(req_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(allow)
        return True

    def pop(self, req_id: str) -> asyncio.Future[bool] | None:
        return self._futures.pop(req_id, None)


def build_slack_permission_callback(
    *,
    pending: PendingPermissions,
    slack_client: Any,
    channel_id: str,
    thread_ts: str,
    timeout_s: int = 300,
    tool_scope: frozenset[str] | None = None,
):
    """Return a CanUseTool async callback bound to a single Slack thread.

    Behavior:
      - `tool_scope` set and tool outside it → Deny immediately (channel policy,
        see policy.py). None = no gate (prod default, every tool allowed).
      - Tool not in confirm whitelist → Allow immediately (passthrough).
      - Tool needs confirm → post Slack block-kit buttons with action_id
        `perm_allow` / `perm_deny`, create a pending Future, await it.
      - Timeout → Deny.

    `allowed_tools` in the brain options is empty, so this callback fires for
    every tool — that's what lets the scope gate cover builtins (Read/Bash/…) too.
    """
    async def cb(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ):
        # MCP tools arrive as `mcp__agentic__<name>`; match the bare suffix so the
        # scope set (bare names) covers both MCP and builtin tools.
        bare = tool_name.rsplit("__", 1)[-1]
        if tool_scope is not None and tool_name not in tool_scope and bare not in tool_scope:
            return PermissionResultDeny(
                behavior="deny",
                message=f"`{bare}` is outside this channel's scope",
            )

        if not _needs_confirm(tool_name, tool_input):
            return PermissionResultAllow(behavior="allow", updated_input=tool_input)

        # ctx.tool_use_id is guaranteed non-None on the can_use_tool path
        # (see SDK types.py:209), but the dataclass keeps it Optional for
        # field-ordering reasons. Coerce to "" defensively.
        tool_use_id = (ctx.tool_use_id or "anon") if ctx else "anon"
        req_id = f"{thread_ts}:{tool_use_id}"
        fut = await pending.create(req_id)

        try:
            input_preview = json.dumps(tool_input or {}, ensure_ascii=False)[:500]
        except Exception:
            input_preview = str(tool_input)[:500]

        try:
            await slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"❓ Allow `{tool_name}` to run?",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"❓ Allow the *{tool_name}* tool to run?\n"
                                f"```{input_preview}```"
                            ),
                        },
                    },
                    {
                        "type": "actions",
                        "block_id": f"perm:{req_id}",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "perm_allow",
                                "text": {"type": "plain_text", "text": "✅ Allow"},
                                "style": "primary",
                                "value": req_id,
                            },
                            {
                                "type": "button",
                                "action_id": "perm_deny",
                                "text": {"type": "plain_text", "text": "❌ Cancel"},
                                "style": "danger",
                                "value": req_id,
                            },
                        ],
                    },
                ],
            )
        except Exception:
            log.exception("permission button post failed; denying tool=%s", tool_name)
            pending.pop(req_id)
            return PermissionResultDeny(
                behavior="deny",
                message="could not send the confirm prompt to Slack",
            )

        try:
            allow = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            pending.pop(req_id)
            return PermissionResultDeny(
                behavior="deny",
                message=f"timeout {timeout_s}s — no response",
            )

        if allow:
            return PermissionResultAllow(behavior="allow", updated_input=tool_input)
        return PermissionResultDeny(behavior="deny", message="user cancelled")

    return cb
