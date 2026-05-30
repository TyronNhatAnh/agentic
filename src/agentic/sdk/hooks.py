"""Brain session hooks (Phase 4 — §12.J).

Tool-lifecycle + compaction hooks bound per Slack thread. They own per-tool
``runs`` logging now that the stream loop no longer collects tool records:

- ``PreToolUse``        — stamp a monotonic start keyed by ``tool_use_id``.
- ``PostToolUse``       — success: pop the start, compute duration, log an ok row.
- ``PostToolUseFailure``— failure: same, but status=error (PostToolUse only
                          fires on success, so failures need their own hook).
- ``PreCompact``        — log-only; the SDK fires this when compaction is
                          already happening, not as an "almost full" forecast.

Hook input is a TypedDict (dict access). Returning ``{}`` is a no-op — these
hooks observe, they do not alter tool execution or input.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from claude_agent_sdk import HookMatcher

from ..store import log_run

log = logging.getLogger(__name__)

# Redact token-like substrings from the *logged* tool-input preview. Audit-only
# — never feeds back into the tool call. Covers GitHub PATs, Slack tokens,
# git http token auth, and userinfo creds in clone/push URLs.
_SECRET_RE = re.compile(
    r"ghp_[A-Za-z0-9]{16,}"
    r"|gh[oprsu]_[A-Za-z0-9]{16,}"
    r"|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|x-access-token:[^@\s/]+"
    r"|//[^/\s:@]+:[^@\s/]+@"
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub("«redacted»", text)


def _preview(payload: Any) -> str:
    try:
        return _redact(json.dumps(payload, ensure_ascii=False)[:500])
    except Exception:
        return _redact(str(payload)[:500])


def build_brain_hooks(*, thread_ts: str, channel: str) -> dict[str, list[HookMatcher]]:
    """Hooks for one brain session. ``starts`` lives for the session lifetime
    (one dict per closure); entries are popped as each tool completes."""
    starts: dict[str, float] = {}

    def _log_tool(inp: dict, tool_use_id: str | None, *, ok: bool, err: str | None) -> None:
        tid = inp.get("tool_use_id") or tool_use_id
        started = starts.pop(tid, None) if tid else None
        duration_ms = int((time.monotonic() - started) * 1000) if started else 0
        try:
            log_run(
                agent=inp.get("tool_name") or "tool",
                input_text=_preview(inp.get("tool_input")),
                output=None,
                status="ok" if ok else "error",
                duration_ms=duration_ms,
                thread_ts=thread_ts,
                channel=channel,
                error=err,
            )
        except Exception:
            log.exception("hook log_run failed thread=%s", thread_ts)

    async def pre_tool(inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        tid = inp.get("tool_use_id") or tool_use_id
        if tid:
            starts[tid] = time.monotonic()
        return {}

    async def post_tool(inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        _log_tool(inp, tool_use_id, ok=True, err=None)
        return {}

    async def post_tool_fail(inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        err = (inp.get("error") or "tool_error")
        _log_tool(inp, tool_use_id, ok=False, err=err[:500] if isinstance(err, str) else str(err)[:500])
        return {}

    async def pre_compact(inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        log.warning(
            "sdk compaction thread=%s trigger=%s", thread_ts, inp.get("trigger")
        )
        return {}

    return {
        "PreToolUse": [HookMatcher(hooks=[pre_tool])],
        "PostToolUse": [HookMatcher(hooks=[post_tool])],
        "PostToolUseFailure": [HookMatcher(hooks=[post_tool_fail])],
        "PreCompact": [HookMatcher(hooks=[pre_compact])],
    }
