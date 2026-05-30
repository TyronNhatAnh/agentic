"""SDK-backed brain session (Phase 2 — §12.F).

One ClaudeSDKClient per Slack thread via a dedicated brain pool. Streams +
records tool_use/tool_result pairs. Brain pool is separate from dev pool
until Phase 3 collapses dev into AgentDefinition.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from ..agents.base import load_prompt
from ..config import settings
from ..store import get_thread, update_thread_fields
from .client_pool import ThreadSessionManager
from .hooks import build_brain_hooks
from .mcp_tools import build_agentic_mcp_server
from .permission import PendingPermissions, build_slack_permission_callback
from .sub_agents import DEV_DISALLOWED_TOOLS, build_subagents

log = logging.getLogger(__name__)

# Slack chat.update is ~1/s/channel — 1.5s leaves headroom for long tails.
_STREAM_EDIT_INTERVAL_S = 1.5
# Appended to every *streaming* edit so a partial reply that sits still (brain is
# mid-tool-call / still thinking) doesn't read as the finished answer. The worker
# renders the final reply via a separate path (job.reply), so it never carries
# this marker — its disappearance is the "done" signal. Added inside
# _safe_placeholder_update (the only streaming-edit site) after truncation so it's
# never clipped, and adds no extra chat.update calls (§8 2026-05-30 rate-limit guard).
_STREAM_SUFFIX = "\n\n_⏳ đang xử lý…_"


@dataclass
class BrainResult:
    reply: str
    session_id: str
    usage: dict
    cost_usd: float
    duration_ms: int
    num_turns: int
    tool_use_count: int = 0
    stop_reason: str | None = None
    error: str | None = None


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
    # Built once at startup; prompt strings + tool lists are pinned into the
    # session prefix so AgentDefinition changes don't churn the cache.
    subagents = build_subagents()

    async def factory(thread_ts: str) -> ClaudeAgentOptions:
        row = get_thread(thread_ts) or {}
        channel = (row.get("channel") or "").strip()
        cwd, add_dirs = _session_dirs(row)
        cb = build_slack_permission_callback(
            pending=pending,
            slack_client=slack_client,
            channel_id=channel,
            thread_ts=thread_ts,
        )
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={"agentic": server},
            permission_mode="default",
            # The brain runs with the full default tool palette (incl. Bash — that's
            # how it does `go build`/git/gh; the dev sub-agent can't get Bash from
            # the SDK, so the brain orchestrates git/build itself). Deny rules are
            # evaluated first and strip the tool from context entirely, closing the
            # history-rewrite hole at the session level: force-push / reset --hard /
            # clean are blocked for the brain too, not just the dev sub-agent.
            disallowed_tools=DEV_DISALLOWED_TOOLS,
            can_use_tool=cb,
            agents=subagents,
            hooks=build_brain_hooks(thread_ts=thread_ts, channel=channel),
            resume=row.get("sdk_session_id") or None,
            session_store=session_store,
            cwd=cwd,
            add_dirs=add_dirs,
        )

    return factory


def _session_dirs(row: dict) -> tuple[str | None, list[str]]:
    """Resolve the session cwd + writable roots for the dev sub-agent.

    The session cwd is locked at session-open (ThreadSessionManager caches the
    client per thread), so a worktree created mid-thread can't change it. We
    therefore (a) open in the thread's active worktree when one already exists,
    else the shared workspace dir, and (b) add both the workspace and worktree
    roots to ``add_dirs`` so dev edits land under acceptEdits even when the
    worktree was created after the session opened — the per-turn worktree path
    still rides in on the workspace hint (§8 2026-05-29)."""
    roots = [d for d in (settings.workspace_dir, settings.worktree_dir) if d]
    worktree = (row.get("active_worktree") or "").strip()
    if worktree and os.path.isdir(worktree):
        cwd: str | None = worktree
        if worktree not in roots:
            roots.append(worktree)
    else:
        cwd = settings.workspace_dir or None
    # Preserve order, drop dups.
    add_dirs = list(dict.fromkeys(roots))
    return cwd, add_dirs


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
    tool_use_count = 0
    result_msg: ResultMessage | None = None
    error: str | None = None

    # Per-tool runs logging now lives in the PostToolUse/PostToolUseFailure hooks
    # (§12.J); the stream loop only buffers text for Slack and counts tool uses
    # for the footer.
    try:
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_use_count += 1
                now = time.monotonic()
                if text_parts and now - last_edit >= _STREAM_EDIT_INTERVAL_S:
                    cooldown = await _safe_placeholder_update(
                        slack_client, channel_id, placeholder_ts,
                        "".join(text_parts),
                    )
                    # On a Slack 429 push the next allowed edit out by Retry-After
                    # so the stream stops hammering chat.update.
                    last_edit = now + cooldown
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
        tool_use_count=tool_use_count,
        stop_reason=result_msg.stop_reason if result_msg else None,
        error=error,
    )


def _format_messages(messages: list[dict]) -> str:
    """Render Slack thread history into a budget-capped transcript for the brain
    user message (inlined from the retired brain.py at Phase 5 cutover)."""
    if not messages:
        return ""
    lines: list[str] = []
    budget = settings.brain_history_budget_chars
    msg_cap = settings.brain_history_msg_cap_chars
    for m in messages:
        role = m.get("role", "?")
        text = (m.get("text") or "").strip()
        line = f"{role}: {text}"
        if len(line) > msg_cap:
            line = line[:msg_cap] + f"\n…[message cắt bớt {len(line) - msg_cap} ký tự]"
        if sum(len(existing) for existing in lines) + len(line) > budget:
            remaining = max(0, budget - sum(len(existing) for existing in lines))
            if remaining > 200:
                lines.append(line[:remaining] + "\n…[history cắt bớt]")
            break
        lines.append(line)
    return "\n".join(lines)


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


async def _safe_placeholder_update(
    client: Any, channel: str, ts: str, text: str
) -> float:
    """Best-effort streaming edit. Returns extra seconds to wait before the next
    edit — non-zero only when Slack rate-limited us (chat.update is ~1/s/channel,
    so a burst can 429). The final, complete reply is rendered by the worker onto
    the same placeholder, so dropping an intermediate edit here is safe."""
    if not (channel and ts):
        return 0.0
    snippet = text.strip()
    if not snippet:
        return 0.0
    if len(snippet) > 3500:
        snippet = snippet[:3400] + "\n…"
    snippet += _STREAM_SUFFIX
    try:
        await client.chat_update(channel=channel, ts=ts, text=snippet)
        return 0.0
    except Exception as e:
        retry_after = _retry_after_seconds(e)
        if retry_after:
            log.warning("chat.update rate-limited; backing off %.1fs", retry_after)
            return retry_after
        log.exception("brain stream placeholder update failed")
        return 0.0


def _retry_after_seconds(exc: Exception) -> float:
    """Extract Retry-After (seconds) from a Slack ratelimited error, else 0.
    Duck-typed so we don't hard-depend on slack_sdk's error class here."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return 0.0
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
