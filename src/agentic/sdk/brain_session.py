"""SDK-backed brain session (Phase 2 — §12.F).

One ClaudeSDKClient per Slack thread via a dedicated brain pool. Streams +
records tool_use/tool_result pairs. Brain pool is separate from dev pool
until Phase 3 collapses dev into AgentDefinition.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..agents.base import load_prompt
from ..brain import _format_messages
from ..store import get_thread, update_thread_fields
from .client_pool import ThreadSessionManager
from .mcp_tools import build_agentic_mcp_server
from .permission import PendingPermissions, build_slack_permission_callback

log = logging.getLogger(__name__)

# Slack chat.update is ~1/s/channel — 1.5s leaves headroom for long tails.
_STREAM_EDIT_INTERVAL_S = 1.5


@dataclass
class ToolCallRecord:
    name: str
    input_preview: str
    ok: bool
    duration_ms: int
    error: str | None = None


@dataclass
class BrainResult:
    reply: str
    session_id: str
    usage: dict
    cost_usd: float
    duration_ms: int
    num_turns: int
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    stop_reason: str | None = None
    error: str | None = None


@dataclass
class _PendingCall:
    name: str
    input_preview: str
    started_at: float


def make_brain_options_factory(
    *,
    pending: PendingPermissions,
    session_store: Any,
    slack_client: Any,
    mcp_server: Any | None = None,
):
    """Factory pinned into the brain ThreadSessionManager. Looks up channel +
    resume token per thread on first session open. `allowed_tools` empty so
    `can_use_tool` actually fires for CONFIRM_TOOLS (§8 2026-05-29)."""
    server = mcp_server or build_agentic_mcp_server()
    system_prompt = load_prompt("brain_sdk")

    async def factory(thread_ts: str) -> ClaudeAgentOptions:
        row = get_thread(thread_ts) or {}
        cb = build_slack_permission_callback(
            pending=pending,
            slack_client=slack_client,
            channel_id=(row.get("channel") or "").strip(),
            thread_ts=thread_ts,
        )
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={"agentic": server},
            permission_mode="default",
            can_use_tool=cb,
            agents={},   # Phase 3 fill
            hooks={},    # Phase 4 fill
            resume=row.get("sdk_session_id") or None,
            session_store=session_store,
        )

    return factory


async def run_brain_session(
    *,
    user_text: str,
    thread_ts: str,
    channel_id: str,
    slack_client: Any,
    placeholder_ts: str,
    thread_history: list[dict],
    workspace_hint: str | None,
    pool: ThreadSessionManager,
    pending: PendingPermissions,  # noqa: ARG001 — resolution lives in factory closure
) -> BrainResult:
    """Run one brain turn. Streams to Slack placeholder (debounced), records
    tool_use/tool_result pairs, returns on terminal ResultMessage. System prompt
    + tools stay constant for cache; workspace_hint + thread_history go in the
    user message only (§12.F cache contract)."""
    t_start = time.monotonic()
    client = await pool.get_or_create(thread_ts)
    await client.query(_compose_user_message(
        user_text=user_text,
        thread_history=thread_history,
        workspace_hint=workspace_hint,
    ))

    text_parts: list[str] = []
    last_edit = 0.0
    pending_calls: dict[str, _PendingCall] = {}
    tool_calls: list[ToolCallRecord] = []
    result_msg: ResultMessage | None = None
    error: str | None = None

    try:
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        pending_calls[block.id] = _PendingCall(
                            name=block.name,
                            input_preview=_preview(block.input),
                            started_at=time.monotonic(),
                        )
                now = time.monotonic()
                if text_parts and now - last_edit >= _STREAM_EDIT_INTERVAL_S:
                    await _safe_placeholder_update(
                        slack_client, channel_id, placeholder_ts,
                        "".join(text_parts),
                    )
                    last_edit = now
            elif isinstance(msg, UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        call = pending_calls.pop(block.tool_use_id, None)
                        if call is not None:
                            tool_calls.append(_close_call(call, block))
            elif isinstance(msg, ResultMessage):
                result_msg = msg
                break
    except Exception as e:
        log.exception("brain session stream failed thread=%s", thread_ts)
        error = str(e) or e.__class__.__name__

    final_text = (
        result_msg.result if result_msg and result_msg.result
        else "".join(text_parts)
    ).strip()
    usage = (result_msg.usage if result_msg else None) or {}
    duration_ms = (result_msg.duration_ms if result_msg and result_msg.duration_ms
                   else int((time.monotonic() - t_start) * 1000))
    session_id = (result_msg.session_id if result_msg else "") or ""
    cost = result_msg.total_cost_usd if result_msg else None
    if result_msg and result_msg.is_error and not error:
        error = result_msg.result or result_msg.stop_reason or "result_error"
    if session_id:
        try:
            update_thread_fields(thread_ts, sdk_session_id=session_id)
        except Exception:
            log.exception("persist sdk_session_id failed thread=%s", thread_ts)

    log.info(
        "sdk brain thread=%s cache_read=%d cache_create=%d in=%d out=%d cost=%s",
        thread_ts, usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0), usage.get("input_tokens", 0),
        usage.get("output_tokens", 0), f"${cost:.4f}" if cost is not None else "?",
    )
    return BrainResult(
        reply=final_text, session_id=session_id, usage=usage,
        cost_usd=cost or 0.0, duration_ms=duration_ms,
        num_turns=(result_msg.num_turns or 0) if result_msg else 0,
        tool_calls=tool_calls,
        stop_reason=result_msg.stop_reason if result_msg else None,
        error=error,
    )


def _preview(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)[:500]
    except Exception:
        return str(payload)[:500]


def _close_call(call: _PendingCall, block: ToolResultBlock) -> ToolCallRecord:
    err: str | None = None
    if block.is_error:
        c = block.content
        err = c[:500] if isinstance(c, str) else _preview(c) if c else "tool_error"
    return ToolCallRecord(
        name=call.name,
        input_preview=call.input_preview,
        ok=not bool(block.is_error),
        duration_ms=int((time.monotonic() - call.started_at) * 1000),
        error=err,
    )


def _compose_user_message(
    *,
    user_text: str,
    thread_history: list[dict],
    workspace_hint: str | None,
) -> str:
    parts: list[str] = []
    rendered = _format_messages(thread_history or [])
    if rendered:
        parts.append("## Bối cảnh thread (lịch sử Slack)\n" + rendered)
    if workspace_hint:
        parts.append("## Workspace hiện tại\n" + workspace_hint.strip())
    parts.append("---\n" + user_text)
    return "\n\n".join(parts)


async def _safe_placeholder_update(client: Any, channel: str, ts: str, text: str) -> None:
    if not (channel and ts):
        return
    snippet = text.strip()
    if not snippet:
        return
    if len(snippet) > 3500:
        snippet = snippet[:3400] + "\n…"
    try:
        await client.chat_update(channel=channel, ts=ts, text=snippet)
    except Exception:
        log.exception("brain stream placeholder update failed")
