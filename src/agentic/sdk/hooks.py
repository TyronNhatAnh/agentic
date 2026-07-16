"""Brain session hooks (Phase 4 — §12.J).

Tool-lifecycle + compaction hooks bound per Slack thread. They own per-tool
``runs`` logging now that the stream loop no longer collects tool records:

- ``PreToolUse``        — stamp a monotonic start keyed by ``tool_use_id``;
                          denies raw network git (``git fetch``/``pull``) in
                          Bash — SSH remote has no key here, see below.
- ``PostToolUse``       — success: pop the start, compute duration, log an ok row.
- ``PostToolUseFailure``— failure: same, but status=error (PostToolUse only
                          fires on success, so failures need their own hook).
- ``PreCompact``        — log-only; the SDK fires this when compaction is
                          already happening, not as an "almost full" forecast.

Hook input is a TypedDict (dict access). Returning ``{}`` is a no-op — apart
from the raw-net-git deny, these hooks observe, they do not alter tool
execution or input.
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


# Raw network git via Bash goes to the repo's configured remote (SSH), which
# has no usable key in the bot's environment — the fetch fails, and when the
# failure is piped away (`git fetch ... | tail`) later ref reads silently
# report stale local state. This produced a real wrong answer (da-api "latest"
# release reported 3 sprints behind, 2026-07-15). Deny and steer to the
# token-based path; an explicit https:// URL *is* the token path and passes.
_RAW_NET_GIT_RE = re.compile(r"\bgit\b[^|;&]*\b(fetch|pull)\b")
_RAW_NET_GIT_REASON = (
    "Raw `git fetch`/`git pull` dùng remote SSH — môi trường bot không có SSH "
    "key nên fail hoặc để lại ref cũ. Dùng `mcp__agentic__git_latest_release` "
    "cho câu hỏi release branch/commit mới nhất, hoặc các tool `git_*`; nếu "
    "bắt buộc fetch thủ công thì fetch qua URL `https://` với GITHUB_TOKEN."
)


def _deny_raw_net_git(tool_name: str | None, tool_input: Any) -> bool:
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command") or ""
    return bool(_RAW_NET_GIT_RE.search(command)) and "https://" not in command


def _preview(payload: Any) -> str:
    try:
        return _redact(json.dumps(payload, ensure_ascii=False)[:500])
    except Exception:
        return _redact(str(payload)[:500])


def build_brain_hooks(*, thread_ts: str, channel: str) -> dict[str, list[HookMatcher]]:
    """Hooks for one brain session. ``starts`` lives for the session lifetime
    (one dict per closure); entries are popped as each tool completes."""
    starts: dict[str, float] = {}

    def _log_tool(
        inp: dict, tool_use_id: str | None, *,
        ok: bool, err: str | None, output: str | None = None,
    ) -> None:
        tid = inp.get("tool_use_id") or tool_use_id
        started = starts.pop(tid, None) if tid else None
        duration_ms = int((time.monotonic() - started) * 1000) if started else 0
        try:
            log_run(
                agent=inp.get("tool_name") or "tool",
                input_text=_preview(inp.get("tool_input")),
                output=output,
                status="ok" if ok else "error",
                duration_ms=duration_ms,
                thread_ts=thread_ts,
                channel=channel,
                error=err,
            )
        except Exception:
            log.exception("hook log_run failed thread=%s", thread_ts)

    async def pre_tool(inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        if _deny_raw_net_git(inp.get("tool_name"), inp.get("tool_input")):
            log.warning("denied raw net git thread=%s", thread_ts)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _RAW_NET_GIT_REASON,
                }
            }
        tid = inp.get("tool_use_id") or tool_use_id
        if tid:
            starts[tid] = time.monotonic()
        return {}

    async def post_tool(inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        # Persist a redacted preview of ``tool_response`` so an ok row is still
        # inspectable after the fact — e.g. a Bash grep that exits 0 with no
        # matches, or a tool that "succeeded" but returned an empty/degenerate
        # payload. Failures carry their text in ``error``; ok rows had none.
        _log_tool(
            inp, tool_use_id, ok=True, err=None,
            output=_preview(inp.get("tool_response")),
        )
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
