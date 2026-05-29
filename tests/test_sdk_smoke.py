"""Phase 0 smoke tests for the SDK integration layer.

What's verified WITHOUT spawning the real `claude` binary:
- claude-agent-sdk is installed and importable
- SqliteSessionStore round-trips entries against the real `threads` table
- ThreadSessionManager calls the options factory + caches per thread_ts
- mcp_tools builds an SDK MCP server config without error

What's NOT verified here (covered in Phase 1 integration tests):
- An actual `claude` subprocess connecting & answering a prompt. Requires
  `claude login` on the host and is too slow / non-hermetic for unit suite.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agentic.sdk import SqliteSessionStore, ThreadSessionManager
from agentic.sdk.mcp_tools import build_agentic_mcp_server
from agentic.store import init_db, touch_thread


def test_sdk_imports():
    """Catch a broken install before Phase 1 work depends on it."""
    import claude_agent_sdk

    assert hasattr(claude_agent_sdk, "ClaudeSDKClient")
    assert hasattr(claude_agent_sdk, "ClaudeAgentOptions")
    assert hasattr(claude_agent_sdk, "tool")
    assert hasattr(claude_agent_sdk, "create_sdk_mcp_server")


def test_build_mcp_server_does_not_crash():
    cfg = build_agentic_mcp_server()
    assert cfg is not None


async def test_session_store_roundtrip():
    init_db()
    thread_ts = "1700000000.000001"
    touch_thread(thread_ts, "C_TEST")

    store = SqliteSessionStore()
    key = {"project_key": thread_ts, "session_id": "sess-abc"}
    entries = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-05-29T00:00:00Z"},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-05-29T00:00:01Z"},
    ]
    await store.append(key, entries)
    loaded = await store.load(key)
    assert loaded == entries

    # Append a second batch — load should return the concatenated transcript.
    extra = [{"type": "user", "uuid": "u2", "timestamp": "2026-05-29T00:00:02Z"}]
    await store.append(key, extra)
    loaded2 = await store.load(key)
    assert loaded2 == entries + extra

    # Different session_id on same thread → load returns None (stale key).
    other = await store.load({"project_key": thread_ts, "session_id": "sess-xyz"})
    assert other is None


async def test_session_store_ignores_subagent_subpath():
    """Phase 0 only persists main transcripts; subagent transcripts are no-op."""
    init_db()
    thread_ts = "1700000000.000002"
    touch_thread(thread_ts, "C_TEST")
    store = SqliteSessionStore()
    key = {
        "project_key": thread_ts,
        "session_id": "sess-sub",
        "subpath": "subagents/agent-1",
    }
    await store.append(key, [{"type": "user", "uuid": "x"}])
    assert await store.load(key) is None


async def test_thread_session_manager_caches_per_thread(monkeypatch):
    """get_or_create called twice for same thread_ts should yield same client
    without re-invoking the options factory or reconnecting."""
    # Patch ClaudeSDKClient inside client_pool to avoid spawning `claude`.
    from agentic.sdk import client_pool as cp

    fake_client = AsyncMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()

    constructor_calls: list = []

    def fake_ctor(options):
        constructor_calls.append(options)
        return fake_client

    monkeypatch.setattr(cp, "ClaudeSDKClient", fake_ctor)

    factory_calls: list[str] = []

    async def factory(thread_ts: str):
        factory_calls.append(thread_ts)
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions()

    mgr = ThreadSessionManager(factory, idle_ttl_s=60, max_sessions=4)
    c1 = await mgr.get_or_create("T1")
    c2 = await mgr.get_or_create("T1")
    c3 = await mgr.get_or_create("T2")
    assert c1 is c2
    assert c1 is fake_client
    assert c3 is fake_client  # same fake, different cache slot
    assert factory_calls == ["T1", "T2"]
    assert len(constructor_calls) == 2

    await mgr.shutdown_all()
    assert fake_client.disconnect.await_count == 2


async def test_thread_session_manager_evicts_lru(monkeypatch):
    from agentic.sdk import client_pool as cp

    fake_client = AsyncMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    monkeypatch.setattr(cp, "ClaudeSDKClient", lambda options: fake_client)

    async def factory(thread_ts: str):
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions()

    mgr = ThreadSessionManager(factory, idle_ttl_s=60, max_sessions=2)
    await mgr.get_or_create("T1")
    await mgr.get_or_create("T2")
    await mgr.get_or_create("T3")  # forces eviction of T1

    # Touch T2 then add T4 — T3 should evict (T2 was just used, T3 oldest).
    await mgr.get_or_create("T2")
    await mgr.get_or_create("T4")

    # Internal state check — only 2 entries, T3 evicted.
    assert set(mgr._clients.keys()) == {"T2", "T4"}

    # Give eviction tasks time to complete close calls.
    await asyncio.sleep(0)
    await mgr.shutdown_all()
