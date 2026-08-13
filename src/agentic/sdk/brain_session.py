"""SDK-backed brain session (Phase 2 — §12.F).

One ClaudeSDKClient per Slack thread via a dedicated brain pool. Streams +
records tool_use/tool_result pairs. Brain pool is separate from dev pool
until Phase 3 collapses dev into AgentDefinition.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from ..agents.base import load_prompt
from ..config import settings
from ..policy import WorkspacePolicy, resolve_policy
from ..store import get_thread, update_thread_fields
from .client_pool import ThreadSessionManager
from .hooks import build_brain_hooks
from .mcp_tools import build_agentic_mcp_server
from .permission import (
    SESSION_DISALLOWED_TOOLS,
    PendingPermissions,
    build_slack_permission_callback,
)
from .sub_agents import build_subagents

log = logging.getLogger(__name__)

# Slack chat.update is ~1/s/channel — 1.5s leaves headroom for long tails.
_STREAM_EDIT_INTERVAL_S = 1.5
# Exactly one status line per in-flight edit, so a frozen partial doesn't read as
# the finished answer (the worker's final reply carries none — that's the "done"
# signal). Applied in _safe_placeholder_update after truncation so it's never
# clipped. One line per writer, never both: they used to stack into two hourglasses.
_STREAM_SUFFIX_FMT = "\n\n_⏳ processing… {}_"
_HEARTBEAT_SUFFIX_FMT = "\n\n_⏳ still working… {}_"
# The stream only edits when the SDK emits something; a stalled API call emits
# nothing (2026-08-07: 344s before the first token) and looks like a dead bot.
_HEARTBEAT_INTERVAL_S = 20


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
    duration_api_ms: int = 0
    ttft_ms: int | None = None


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
    # Prompts for every policy loaded once at startup so the per-channel system
    # prompt is pinned into the prefix cache (channel is fixed per thread, so each
    # session still has a stable prefix). Prod uses brain_sdk.
    prompt_cache: dict[str, str] = {}

    def _prompt(name: str) -> str:
        if name not in prompt_cache:
            prompt_cache[name] = load_prompt(name)
        return prompt_cache[name]

    # Built once at startup; prompt strings + tool lists are pinned into the
    # session prefix so AgentDefinition changes don't churn the cache.
    subagents = build_subagents()

    async def factory(thread_ts: str) -> ClaudeAgentOptions:
        row = get_thread(thread_ts) or {}
        channel = (row.get("channel") or "").strip()
        policy = resolve_policy(channel)
        cwd, add_dirs = _session_dirs(row, policy)
        cb = build_slack_permission_callback(
            pending=pending,
            slack_client=slack_client,
            channel_id=channel,
            thread_ts=thread_ts,
            tool_scope=policy.tool_scope,
        )
        agents = (
            subagents
            if policy.subagents is None
            else {k: v for k, v in subagents.items() if k in policy.subagents}
        )
        return ClaudeAgentOptions(
            system_prompt=_prompt(policy.system_prompt),
            mcp_servers={"agentic": server},
            # Both pinned, not left to the CLI default (see config.py).
            model=settings.brain_model,
            **({"effort": settings.brain_effort} if settings.brain_effort else {}),
            # Token-level StreamEvents; without them a tool-heavy turn shows nothing
            # but the tool/heartbeat line until the very end.
            include_partial_messages=True,
            permission_mode="default",
            **_loop_caps(),
            # Deny rules strip the tool from context before can_use_tool, so this
            # closes history-rewrite for the brain itself, not just the dev sub-agent.
            disallowed_tools=SESSION_DISALLOWED_TOOLS,
            can_use_tool=cb,
            agents=agents,
            hooks=build_brain_hooks(thread_ts=thread_ts, channel=channel),
            # The SDK derives SessionKey.project_key from cwd, so an unbound store
            # would key every thread on the repo path — see session_store docstring.
            resume=row.get("sdk_session_id") or None,
            session_store=(
                session_store.for_thread(thread_ts)
                if hasattr(session_store, "for_thread")
                else session_store
            ),
            # Empty, not unset: the CLI's default (user+project+local) pulled the
            # host's personal ~/.claude skills into the bot's prefix.
            setting_sources=[],
            cwd=cwd,
            add_dirs=add_dirs,
        )

    return factory


def _loop_caps() -> dict[str, Any]:
    """SDK-native per-turn caps, omitted when set to 0 so an operator can disable
    an individual cap (an absent kwarg leaves the SDK default = unbounded)."""
    caps: dict[str, Any] = {}
    if settings.brain_max_turns > 0:
        caps["max_turns"] = settings.brain_max_turns
    if settings.brain_max_budget_usd > 0:
        caps["max_budget_usd"] = settings.brain_max_budget_usd
    return caps


def _session_dirs(row: dict, policy: WorkspacePolicy) -> tuple[str | None, list[str]]:
    """Resolve the session cwd + writable roots for the dev sub-agent.

    cwd is anchored to a *stable per-thread constant* — the tier repo root when
    the channel pins one, else the shared workspace dir — and deliberately NOT
    the thread's ``active_worktree``. The bundled CLI keys resumable sessions by
    cwd (it hashes cwd into the ``~/.claude/projects/<cwd>`` dir it looks
    ``--resume`` up under), so tying cwd to a value that only appears mid-thread
    (a worktree created during review) diverges the resume key from the
    session-open key: after idle eviction the next turn re-opens with cwd=worktree,
    the CLI can't find the session under that path ("No conversation found"),
    exits 1, and ``client.connect()`` raises ProcessError → the whole turn dies.
    Keeping cwd constant makes the resume key stable across evict/re-create.

    Worktree writability does not need cwd: dev edits land via ``add_dirs`` under
    acceptEdits, and the brain reaches the worktree through the per-turn workspace
    hint. So the worktree is added to ``add_dirs`` (writable) but never becomes cwd.

    ``policy.repo_roots`` adds tier-specific readable roots — empty for prod, but
    a future channel could pin its own repo here so Read/Glob/Grep can reach it."""
    # Slack image uploads live here; without it Read can't reach the path the
    # user's screenshot was saved to.
    roots = [
        d
        for d in (settings.workspace_dir, settings.worktree_dir, settings.attachment_dir)
        if d
    ]
    roots.extend(r for r in policy.repo_roots if r)
    worktree = (row.get("active_worktree") or "").strip()
    if worktree and os.path.isdir(worktree) and worktree not in roots:
        # Writable via add_dirs — NOT selected as cwd (see docstring).
        roots.append(worktree)
    if policy.repo_roots and os.path.isdir(policy.repo_roots[0]):
        # Tier that pins its own repo: open the session inside it so Read/Glob/Grep
        # operate on that source by default. Constant per channel, so the resume key
        # stays stable. (No tier does this today — prod's repo_roots is empty.)
        cwd: str | None = policy.repo_roots[0]
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
    # Token deltas, display-only. text_parts stays the authoritative reply text —
    # partial events can be dropped/reordered, ResultMessage.result can't.
    live: list[str] = []
    progress = _Progress(slack_client, channel_id, placeholder_ts, t_start)
    tool_use_count = 0
    last_tool_label = ""
    result_msg: ResultMessage | None = None
    error: str | None = None
    # Everything before the first token is waiting, not generating — the half of a
    # slow turn `duration_api_ms` alone can't tell you about.
    ttft_ms: int | None = None

    # Wall-clock breaker for a stalled stream — the SDK-native caps only stop a
    # *looping* agent. 0 disables it; on expiry the pooled client is discarded below.
    deadline = settings.brain_timeout_s if settings.brain_timeout_s > 0 else None
    timed_out = False
    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(progress, stop_heartbeat))
    try:
        async with asyncio.timeout(deadline):
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    # Main-agent text deltas only — sub-agent streams carry
                    # parent_tool_use_id and would interleave into the placeholder.
                    if msg.parent_tool_use_id:
                        continue
                    ev = msg.event or {}
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            if ttft_ms is None:
                                ttft_ms = int((time.monotonic() - t_start) * 1000)
                            live.append(delta["text"])
                            await progress.render("".join(live))
                elif isinstance(msg, AssistantMessage):
                    if ttft_ms is None:  # no partials on this turn
                        ttft_ms = int((time.monotonic() - t_start) * 1000)
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_use_count += 1
                            last_tool_label = _tool_label(block.name)
                    # Fallback for a turn without partials; before any prose exists,
                    # show tool activity so the placeholder isn't frozen through the
                    # brain's front-loaded tool phase.
                    view = "".join(live) or "".join(text_parts) or _tool_progress(
                        last_tool_label, tool_use_count
                    )
                    await progress.render(view)
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
                    break
    except TimeoutError:
        timed_out = True
        log.warning("brain session timed out after %ss thread=%s", deadline, thread_ts)
        error = (
            f"processing timed out ({deadline}s) — the session was reset, "
            "please resend your request"
        )
    except Exception as e:
        log.exception("brain session stream failed thread=%s", thread_ts)
        error = str(e) or e.__class__.__name__
    finally:
        stop_heartbeat.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await progress.aclose()

    # The pooled client's in-flight receive_response generator is left pending on a
    # timeout — reusing it next turn would interleave two streams. Discard it; the
    # persisted resume token reopens a clean session with the same history.
    if timed_out:
        try:
            await pool.release(thread_ts)
        except Exception:
            log.exception("releasing timed-out brain client failed thread=%s", thread_ts)

    final_text = (
        result_msg.result if result_msg and result_msg.result
        else "".join(text_parts)
    ).strip()
    usage = _turn_usage(result_msg)
    duration_ms = (result_msg.duration_ms if result_msg and result_msg.duration_ms
                   else int((time.monotonic() - t_start) * 1000))
    session_id = (result_msg.session_id if result_msg else "") or ""
    cost = result_msg.total_cost_usd if result_msg else None
    if result_msg and result_msg.is_error and not error:
        # A per-turn cap flags is_error but reports the last turn's *natural*
        # stop_reason, so these three values are how a cap is recognised at all.
        if (result_msg.stop_reason or "") in {"tool_use", "end_turn", "max_tokens"}:
            error = (
                "hit the per-turn safety limit (step count or cost) — "
                "the reply may be incomplete, say 'continue' to finish it"
            )
            log.warning(
                "brain hit per-turn cap thread=%s stop=%s turns=%s cost=%s",
                thread_ts, result_msg.stop_reason, result_msg.num_turns,
                result_msg.total_cost_usd,
            )
        else:
            error = result_msg.result or result_msg.stop_reason or "result_error"
            # Bare "result_error" (both fields empty) is undiagnosable later, so
            # dump whatever else the ResultMessage carried.
            log.error(
                "brain result error thread=%s stop=%s subtype=%s turns=%s "
                "cost=%s session=%s result=%r",
                thread_ts, result_msg.stop_reason,
                getattr(result_msg, "subtype", None), result_msg.num_turns,
                result_msg.total_cost_usd, session_id,
                (result_msg.result or "")[:500],
            )
    if session_id:
        try:
            update_thread_fields(thread_ts, sdk_session_id=session_id)
        except Exception:
            log.exception("persist sdk_session_id failed thread=%s", thread_ts)

    api_ms = (result_msg.duration_api_ms or 0) if result_msg else 0
    log.info(
        "sdk brain thread=%s cache_read=%d cache_create=%d in=%d out=%d cost=%s "
        "wall=%dms api=%dms ttft=%s",
        thread_ts, usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0), usage.get("input_tokens", 0),
        usage.get("output_tokens", 0), f"${cost:.4f}" if cost is not None else "?",
        duration_ms, api_ms, f"{ttft_ms}ms" if ttft_ms is not None else "-",
    )
    return BrainResult(
        reply=final_text, session_id=session_id, usage=usage,
        cost_usd=cost or 0.0, duration_ms=duration_ms,
        num_turns=(result_msg.num_turns or 0) if result_msg else 0,
        tool_use_count=tool_use_count,
        stop_reason=result_msg.stop_reason if result_msg else None,
        error=error,
        duration_api_ms=api_ms,
        ttft_ms=ttft_ms,
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
            line = line[:msg_cap] + f"\n…[message truncated {len(line) - msg_cap} chars]"
        if sum(len(existing) for existing in lines) + len(line) > budget:
            remaining = max(0, budget - sum(len(existing) for existing in lines))
            if remaining > 200:
                lines.append(line[:remaining] + "\n…[history truncated]")
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
        parts.append("## Thread context (Slack history)\n" + rendered)
    if workspace_hint:
        parts.append("## Current workspace\n" + workspace_hint.strip())
    parts.append("---\n" + user_text)
    return "\n\n".join(parts)


def _tool_label(name: str) -> str:
    """Human-readable tool name for the progress line. MCP tools arrive as
    ``mcp__agentic__grafana_query_loki`` — keep only the verb; native tools
    (Bash/Read/Task) pass through unchanged."""
    return name.rsplit("__", 1)[-1] if name else "tool"


def _tool_progress(tool_label: str, count: int) -> str:
    """Progress line shown while the brain is calling tools and hasn't emitted
    prose yet. Empty before the first tool, so the placeholder keeps the initial
    text until there's something real to report. The "processing…" marker is
    added by _safe_placeholder_update, so it's not repeated here."""
    if not tool_label:
        return ""
    steps = f" · {count} steps" if count > 1 else ""
    return f"🔧 running `{tool_label}`{steps}"


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


_MODEL_USAGE_KEYS = (  # snake_case (runs columns) ← camelCase (model_usage)
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_input_tokens", "cacheReadInputTokens"),
    ("cache_creation_input_tokens", "cacheCreationInputTokens"),
)


def _turn_usage(result_msg: ResultMessage | None) -> dict:
    """Whole-turn token totals. `usage` counts the main agent only, while
    `total_cost_usd` bills sub-agents too (2026-08-12: 79k tok vs $1.81, 12× off),
    so sum `model_usage` — it's per-model and includes them."""
    if result_msg is None:
        return {}
    by_model = result_msg.model_usage or {}
    if not by_model:
        return result_msg.usage or {}
    return {
        snake: sum(int(m.get(camel) or 0) for m in by_model.values())
        for snake, camel in _MODEL_USAGE_KEYS
    }


class _Progress:
    """Single owner of the placeholder edit slot, shared by the stream loop and
    the heartbeat. The lock keeps the two from interleaving `chat.update` calls
    on the same message; the debounce is the Slack rate-limit guard (§8)."""

    def __init__(self, client: Any, channel: str, ts: str, t_start: float) -> None:
        self._client = client
        self._channel = channel
        self._ts = ts
        self._t_start = t_start
        self._lock = asyncio.Lock()
        self._last_edit = 0.0
        self._last_rendered = ""
        self._pending = ""
        self._flush: asyncio.Task | None = None

    async def render(self, view: str) -> None:
        """Stream edit, debounced with a trailing flush."""
        async with self._lock:
            now = time.monotonic()
            if not view or view == self._last_rendered:
                return
            wait = _STREAM_EDIT_INTERVAL_S - (now - self._last_edit)
            if wait > 0:
                # Hold, don't drop: the stream usually stops right before a tool
                # call, so a dropped view freezes Slack on the first token.
                self._pending = view
                if self._flush is None or self._flush.done():
                    self._flush = asyncio.create_task(self._flush_pending(wait))
                return
            await self._push(view, now)
            self._last_rendered = view
            self._pending = ""

    async def _flush_pending(self, wait: float) -> None:
        while True:
            await asyncio.sleep(wait)
            async with self._lock:
                view = self._pending
                if not view or view == self._last_rendered:
                    self._pending = ""
                    return
                wait = _STREAM_EDIT_INTERVAL_S - (time.monotonic() - self._last_edit)
                if wait > 0:  # a render or the heartbeat pushed meanwhile
                    continue
                await self._push(view, time.monotonic())
                self._last_rendered = view
                self._pending = ""
                return

    async def aclose(self) -> None:
        """Drop any scheduled flush. The caller writes the final reply into this
        same message right after the turn, and a late partial would overwrite it."""
        if self._flush and not self._flush.done():
            self._flush.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush

    async def render_heartbeat(self) -> None:
        """Refresh the elapsed time, only when the placeholder has gone stale.
        Re-pushes the current view unchanged — the differing status suffix is the
        whole edit. Keeps `_last_rendered` untouched so the stream's dedupe still
        compares against real content."""
        async with self._lock:
            now = time.monotonic()
            if now - self._last_edit < _HEARTBEAT_INTERVAL_S:
                return
            await self._push(self._last_rendered, now, heartbeat=True)

    async def _push(self, view: str, now: float, *, heartbeat: bool = False) -> None:
        cooldown = await _safe_placeholder_update(
            self._client, self._channel, self._ts, view,
            elapsed_s=now - self._t_start,
            suffix_fmt=_HEARTBEAT_SUFFIX_FMT if heartbeat else _STREAM_SUFFIX_FMT,
        )
        # On a Slack 429 push the next allowed edit out by Retry-After so we stop
        # hammering chat.update.
        self._last_edit = now + cooldown


async def _heartbeat(progress: _Progress, stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL_S)
            return
        except TimeoutError:
            pass
        try:
            await progress.render_heartbeat()
        except Exception:  # a failed edit must never kill the turn
            log.exception("heartbeat placeholder update failed")


async def _safe_placeholder_update(
    client: Any, channel: str, ts: str, text: str,
    *, elapsed_s: float, suffix_fmt: str,
) -> float:
    """Best-effort streaming edit. Returns extra seconds to wait before the next
    edit — non-zero only when Slack rate-limited us (chat.update is ~1/s/channel,
    so a burst can 429). The final, complete reply is rendered by the worker onto
    the same placeholder, so dropping an intermediate edit here is safe."""
    if not (channel and ts):
        return 0.0
    suffix = suffix_fmt.format(_fmt_elapsed(elapsed_s))
    snippet = text.strip()
    if not snippet:
        # No prose yet (the heartbeat fires before the first token too) — the
        # status line is the whole message, so drop its leading blank line.
        snippet = suffix.lstrip("\n")
    else:
        # Block Kit markdown blocks cap at 12,000 chars; leave headroom for suffix.
        if len(snippet) > 11500:
            snippet = snippet[:11400] + "\n…"
        snippet += suffix
    try:
        # Render via a Block Kit markdown block so the streamed partial shows
        # formatted (headings/lists/tables) instead of raw markdown; Slack
        # converts standard markdown server-side. `text` is the notify fallback.
        await client.chat_update(
            channel=channel,
            ts=ts,
            text="⏳ processing…",
            blocks=[{"type": "markdown", "text": snippet}],
        )
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
