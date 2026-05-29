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
Phase 2 populates CONFIRM_TOOLS with `github_merge_pr` / `github_approve_pr`
once the MCP server exposes them — those won't be in `allowed_tools` so the
callback will actually fire. If we ever need to gate an already-allowed tool,
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

# Tools that always require user confirm via Slack button.
# Phase 1: empty. Phase 2 populates when MCP wrappers land.
CONFIRM_TOOLS: set[str] = set()

# Bash commands whose `command` field, when prefix-matched, triggers confirm.
# Phase 1: empty (push allowed inline). Phase 2 may revisit.
CONFIRM_BASH_PATTERNS: tuple[str, ...] = ()


def _needs_confirm(tool_name: str, tool_input: dict[str, Any] | None) -> bool:
    if tool_name in CONFIRM_TOOLS:
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
):
    """Return a CanUseTool async callback bound to a single Slack thread.

    Behavior:
      - Tool not in confirm whitelist → Allow immediately (passthrough).
      - Tool needs confirm → post Slack block-kit buttons with action_id
        `perm_allow` / `perm_deny`, create a pending Future, await it.
      - Timeout → Deny.
    """
    async def cb(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ):
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
                text=f"❓ Cho phép `{tool_name}` chạy?",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"❓ Cho phép tool *{tool_name}* chạy?\n"
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
                                "text": {"type": "plain_text", "text": "✅ Cho phép"},
                                "style": "primary",
                                "value": req_id,
                            },
                            {
                                "type": "button",
                                "action_id": "perm_deny",
                                "text": {"type": "plain_text", "text": "❌ Huỷ"},
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
                message="không gửi được prompt confirm lên Slack",
            )

        try:
            allow = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            pending.pop(req_id)
            return PermissionResultDeny(
                behavior="deny",
                message=f"timeout {timeout_s}s — không có phản hồi",
            )

        if allow:
            return PermissionResultAllow(behavior="allow", updated_input=tool_input)
        return PermissionResultDeny(behavior="deny", message="user huỷ")

    return cb
