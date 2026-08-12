"""SDK-backed brain session (Phase 2 — §12.F).

One ClaudeSDKClient per Slack thread via a dedicated brain pool. Streams +
records tool_use/tool_result pairs. Brain pool is separate from dev pool
until Phase 3 collapses dev into AgentDefinition.
"""

from __future__ import annotations

import asyncio
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
# Appended to every *streaming* edit so a partial reply that sits still (brain is
# mid-tool-call / still thinking) doesn't read as the finished answer. The worker
# renders the final reply via a separate path (job.reply), so it never carries
# this marker — its disappearance is the "done" signal. Added inside
# _safe_placeholder_update (the only streaming-edit site) after truncation so it's
# never clipped, and adds no extra chat.update calls (§8 2026-05-30 rate-limit guard).
_STREAM_SUFFIX = "\n\n_⏳ processing…_"
# The stream only edits the placeholder when the SDK emits a message. A stalled
# API call emits nothing (2026-08-07: one turn sat 344s between query and first
# token), leaving the placeholder frozen on the initial text — indistinguishable
# from a dead bot. The heartbeat edits in that gap with the elapsed time.
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
            # Pin the brain model explicitly (was unset → ran on the CLI default).
            # Default Opus for reasoning quality; tunable via BRAIN_MODEL if cost
            # matters more than depth on a given deployment.
            model=settings.brain_model,
            permission_mode="default",
            # SDK-native circuit breakers — the agent loop stops itself before the
            # wall-clock deadline in run_brain_session fires. 0 → leave unset
            # (SDK default = unbounded) so an operator can opt out per cap.
            **_loop_caps(),
            # The brain runs with the full default tool palette (incl. Bash — that's
            # how it does `go build`/git/gh; the dev sub-agent can't get Bash from
            # the SDK, so the brain orchestrates git/build itself). Deny rules are
            # evaluated first and strip the tool from context entirely, closing the
            # history-rewrite hole at the session level: force-push / reset --hard /
            # clean are blocked for the brain too, not just the dev sub-agent.
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
            # Empty = load no user/project/local settings. Unset would make the CLI
            # fall back to its own default (user+project+local), which pulled the
            # host's personal ~/.claude skills listing into the bot's prefix — off
            # topic, and its Vietnamese trigger phrases drifted the reply language.
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
    roots = [d for d in (settings.workspace_dir, settings.worktree_dir) if d]
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
    progress = _Progress(slack_client, channel_id, placeholder_ts, t_start)
    tool_use_count = 0
    last_tool_label = ""
    result_msg: ResultMessage | None = None
    error: str | None = None

    # Per-tool runs logging now lives in the PostToolUse/PostToolUseFailure hooks
    # (§12.J); the stream loop only buffers text for Slack and counts tool uses
    # for the footer.
    # Wall-clock circuit breaker: a hung SDK subprocess must not pin this worker
    # forever (the SDK-native max_turns/max_budget caps stop a *looping* agent, but
    # not a stalled stream). 0 disables the deadline. On expiry the pooled client is
    # discarded below — its receive stream is half-consumed and unsafe to reuse.
    deadline = settings.brain_timeout_s if settings.brain_timeout_s > 0 else None
    timed_out = False
    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(progress, stop_heartbeat))
    try:
        async with asyncio.timeout(deadline):
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_use_count += 1
                            last_tool_label = _tool_label(block.name)
                    # Stream the brain's prose once it starts; until then surface
                    # tool activity so the placeholder reflects progress instead of
                    # freezing on the initial "Processing…" for the whole tool phase
                    # (the brain front-loads Loki/GitHub/git calls before writing).
                    view = "".join(text_parts) or _tool_progress(
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
    usage = (result_msg.usage if result_msg else None) or {}
    duration_ms = (result_msg.duration_ms if result_msg and result_msg.duration_ms
                   else int((time.monotonic() - t_start) * 1000))
    session_id = (result_msg.session_id if result_msg else "") or ""
    cost = result_msg.total_cost_usd if result_msg else None
    if result_msg and result_msg.is_error and not error:
        # A per-turn cap (max_turns / max_budget_usd) makes the SDK flag the result
        # as an error while reporting the last turn's natural stop_reason
        # (`tool_use` when cut mid-tool-loop, `end_turn`/`max_tokens` otherwise).
        # Surface a human note instead of the cryptic raw reason, and keep whatever
        # partial reply we streamed — the answer is truncated, not lost.
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
            # Bare "result_error" (both fields empty) is undiagnosable after the
            # fact — one turn logged exactly that and left nothing to go on. Dump
            # what the ResultMessage did carry.
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

    async def render(self, view: str) -> None:
        """Stream edit — skipped when the content hasn't changed or we edited
        within the debounce window."""
        async with self._lock:
            now = time.monotonic()
            if (
                not view
                or view == self._last_rendered
                or now - self._last_edit < _STREAM_EDIT_INTERVAL_S
            ):
                return
            await self._push(view, now)
            self._last_rendered = view

    async def render_heartbeat(self) -> None:
        """Elapsed-time edit, only when the placeholder has gone stale. Keeps
        `_last_rendered` untouched so the stream's dedupe still compares against
        real content, not the heartbeat line."""
        async with self._lock:
            now = time.monotonic()
            if now - self._last_edit < _HEARTBEAT_INTERVAL_S:
                return
            waiting = f"⏳ waiting for the model… {_fmt_elapsed(now - self._t_start)}"
            base = self._last_rendered
            await self._push(f"{base}\n\n{waiting}" if base else waiting, now)

    async def _push(self, view: str, now: float) -> None:
        cooldown = await _safe_placeholder_update(
            self._client, self._channel, self._ts, view
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
    # Block Kit markdown blocks cap at 12,000 chars; leave headroom for suffix.
    if len(snippet) > 11500:
        snippet = snippet[:11400] + "\n…"
    snippet += _STREAM_SUFFIX
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
