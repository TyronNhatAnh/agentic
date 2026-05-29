"""Phase 1 smoke tests for the SDK dev path.

Hermetic — no real `claude` subprocess, no Slack network. Asserts:
- PendingPermissions Future create/resolve/timeout semantics
- _needs_confirm wiring (whitelists empty by default in Phase 1)
- build_dev_options shape (allow/deny lists, acceptEdits, resume from DB)
- run_dev_sdk streams AssistantMessage text + persists session_id from ResultMessage
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentic.sdk import PendingPermissions, run_dev_sdk
from agentic.sdk.dev_agent import make_dev_options_factory
from agentic.sdk.dev_options import (
    DEV_ALLOWED_TOOLS,
    DEV_DISALLOWED_TOOLS,
    build_dev_options,
)
from agentic.sdk.permission import (
    CONFIRM_BASH_PATTERNS,
    CONFIRM_TOOLS,
    _needs_confirm,
    build_slack_permission_callback,
)
from agentic.store import get_thread, init_db, touch_thread, update_thread_fields


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


def test_phase1_whitelists_empty():
    """Phase 1 ships empty whitelists — SDK skips can_use_tool for already-
    allowed tools (per types.py:1748), so populating CONFIRM_BASH_PATTERNS
    here would be a no-op. Documented in §8 / module docstring."""
    assert CONFIRM_TOOLS == set()
    assert CONFIRM_BASH_PATTERNS == ()
    assert _needs_confirm("Bash", {"command": "git push origin main"}) is False
    assert _needs_confirm("github_merge_pr", {}) is False


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
    # Temporarily populate the whitelist for this test only.
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


def test_build_dev_options_shape():
    init_db()
    thread_ts = "1700000000.dev01"
    touch_thread(thread_ts, "C_TEST")

    opts = build_dev_options(
        thread_ts=thread_ts,
        cwd=None,
        permission_cb=None,
        session_store=None,
    )
    assert opts.permission_mode == "acceptEdits"
    assert opts.allowed_tools == DEV_ALLOWED_TOOLS
    assert opts.disallowed_tools == DEV_DISALLOWED_TOOLS
    assert opts.include_partial_messages is True
    # No prior session_id persisted yet → resume is None.
    assert opts.resume is None


def test_build_dev_options_resume_from_thread_row():
    init_db()
    thread_ts = "1700000000.dev02"
    touch_thread(thread_ts, "C_TEST")
    update_thread_fields(thread_ts, sdk_session_id="prev-session-uuid")

    opts = build_dev_options(
        thread_ts=thread_ts,
        cwd=None,
        permission_cb=None,
        session_store=None,
    )
    assert opts.resume == "prev-session-uuid"


async def test_run_dev_sdk_streams_and_persists_session_id(monkeypatch):
    """Drive run_dev_sdk against a fake pool/client that yields a tiny
    AssistantMessage stream + ResultMessage; assert session_id lands in DB
    and the placeholder gets at least one chat_update."""
    init_db()
    thread_ts = "1700000000.dev03"
    touch_thread(thread_ts, "C_TEST")

    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    asst = AssistantMessage(
        content=[TextBlock(text="đã sửa xong file foo.py")], model="opus"
    )
    result = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="new-session-uuid",
        result="đã sửa xong file foo.py",
        usage={"input_tokens": 1, "output_tokens": 1},
        total_cost_usd=0.001,
    )

    async def fake_stream():
        yield asst
        yield result

    fake_client = SimpleNamespace(
        query=AsyncMock(),
        receive_response=lambda: fake_stream(),
    )

    pool = SimpleNamespace(
        get_or_create=AsyncMock(return_value=fake_client),
    )

    # Force the streaming edit by zeroing the debounce window.
    from agentic.sdk import dev_agent as da
    monkeypatch.setattr(da, "_STREAM_EDIT_INTERVAL_S", 0.0)

    slack = AsyncMock()
    slack.chat_update = AsyncMock(return_value={"ok": True})

    out = await run_dev_sdk(
        "task: fix foo.py",
        thread_ts=thread_ts,
        channel_id="C_TEST",
        slack_client=slack,
        placeholder_ts="9999.0001",
        cwd=None,
        context="",
        pool=pool,
        pending=PendingPermissions(),
    )

    assert out == "đã sửa xong file foo.py"
    slack.chat_update.assert_awaited()
    fake_client.query.assert_awaited_once()
    row = get_thread(thread_ts)
    assert row["sdk_session_id"] == "new-session-uuid"


async def test_options_factory_resolves_channel_from_thread(monkeypatch):
    """Factory should pull channel out of the threads table so the permission
    callback posts buttons to the right place."""
    init_db()
    thread_ts = "1700000000.dev04"
    touch_thread(thread_ts, "C_FACTORY")

    captured: dict = {}

    def fake_build_cb(*, pending, slack_client, channel_id, thread_ts):
        captured["channel_id"] = channel_id
        captured["thread_ts"] = thread_ts
        return "CB-SENTINEL"

    from agentic.sdk import dev_agent as da

    monkeypatch.setattr(da, "build_slack_permission_callback", fake_build_cb)

    factory = make_dev_options_factory(
        pending=PendingPermissions(),
        session_store=None,
        slack_client=AsyncMock(),
    )
    opts = await factory(thread_ts)
    assert captured == {"channel_id": "C_FACTORY", "thread_ts": thread_ts}
    assert opts.can_use_tool == "CB-SENTINEL"
