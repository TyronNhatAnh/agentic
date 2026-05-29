"""SDK-backed dev agent entry point (Phase 1 — §12.B).

Replaces the per-call `claude -p` subprocess in agents/dev.py with a long-lived
ClaudeSDKClient leased from ThreadSessionManager. The session stays open across
turns so the prompt cache + conversation history survive — the whole point of
Phase 1.

Streaming: AssistantMessage TextBlocks are concatenated and the Slack
placeholder is edited at a 1.5s debounce so the user sees progress without
hitting `chat.update` rate limits. Final ResultMessage's session_id is
persisted to threads.sdk_session_id for cross-restart resume.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
)

from ..config import settings
from ..store import get_thread, update_thread_fields
from .client_pool import ThreadSessionManager
from .dev_options import build_dev_options
from .permission import PendingPermissions, build_slack_permission_callback

log = logging.getLogger(__name__)

# Debounce window for streaming placeholder edits. Slack's chat.update limit
# is roughly 1 update/sec/channel — 1.5s gives headroom for the long-tail.
_STREAM_EDIT_INTERVAL_S = 1.5


def make_dev_options_factory(
    *,
    pending: PendingPermissions,
    session_store: Any,
    slack_client: Any,
):
    """Build the OptionsFactory passed to ThreadSessionManager.

    Called once at startup; the returned factory is invoked lazily by the pool
    each time a new thread opens its first session. Resolves the Slack
    `channel_id` from the threads table (set by `touch_thread` when the user
    first mentions the bot) so the permission callback can post buttons back
    to the same channel.
    """

    async def factory(thread_ts: str) -> ClaudeAgentOptions:
        row = get_thread(thread_ts)
        channel_id = ((row or {}).get("channel") or "").strip()
        cb = build_slack_permission_callback(
            pending=pending,
            slack_client=slack_client,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        return build_dev_options(
            thread_ts=thread_ts,
            cwd=None,  # session cwd locked to workspace_dir — see module docstring
            permission_cb=cb,
            session_store=session_store,
        )

    return factory


async def run_dev_sdk(
    task: str,
    *,
    thread_ts: str,
    channel_id: str,
    slack_client: Any,
    placeholder_ts: str,
    cwd: str | None,
    context: str = "",
    pool: ThreadSessionManager,
    pending: PendingPermissions,  # noqa: ARG001 — kept in signature per §12.B; resolution happens inside the cb closure
) -> str:
    """Run one dev turn through a thread-pooled ClaudeSDKClient.

    `cwd` here is informational. The pool's options were built with
    cwd=workspace_dir + add_dirs=[worktree_dir]; per-turn worktree info travels
    in the user prompt as a context block so Claude can address files via
    absolute paths. See §8 decision for the rationale.
    """
    client = await pool.get_or_create(thread_ts)

    prompt = _compose_prompt(task=task, cwd=cwd, context=context)
    await client.query(prompt)

    text_parts: list[str] = []
    last_edit = 0.0
    final_text: str | None = None

    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    text_parts.append(block.text)
            now = time.monotonic()
            if text_parts and now - last_edit >= _STREAM_EDIT_INTERVAL_S:
                await _safe_placeholder_update(
                    slack_client, channel_id, placeholder_ts, "".join(text_parts)
                )
                last_edit = now
        elif isinstance(msg, ResultMessage):
            if msg.session_id:
                try:
                    update_thread_fields(thread_ts, sdk_session_id=msg.session_id)
                except Exception:
                    log.exception(
                        "persist sdk_session_id failed thread=%s", thread_ts
                    )
            # Prefer the canonical `result` field when present — it's the
            # CLI's own final-text rendering and matches what `claude -p`
            # returned, so callers downstream of run_dev_sdk see no diff.
            if msg.result:
                final_text = msg.result
            usage = msg.usage or {}
            log.info(
                "sdk dev usage thread=%s cache_read=%d cache_create=%d in=%d out=%d cost=%s",
                thread_ts,
                usage.get("cache_read_input_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                f"${msg.total_cost_usd:.4f}" if msg.total_cost_usd is not None else "?",
            )

    return final_text if final_text is not None else "".join(text_parts).strip()


def _compose_prompt(*, task: str, cwd: str | None, context: str) -> str:
    parts = [task]
    if cwd and cwd != (settings.workspace_dir or None):
        parts.insert(0, f"## Worktree (cwd hiệu lực cho turn này)\n`{cwd}`\n")
    if context:
        parts.append(f"\n---\nContext:\n{context}")
    return "\n".join(parts)


async def _safe_placeholder_update(
    slack_client: Any, channel: str, ts: str, text: str
) -> None:
    if not (channel and ts):
        return
    snippet = text.strip()
    if not snippet:
        return
    # Slack chat.update text cap is generous; trim defensively for streaming.
    if len(snippet) > 3500:
        snippet = snippet[:3400] + "\n…"
    try:
        await slack_client.chat_update(channel=channel, ts=ts, text=snippet)
    except Exception:
        log.exception("dev stream placeholder update failed")
