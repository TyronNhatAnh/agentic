"""Tests for the architecture-audit fixes.

Covers, one block per fix:
  #1 per-turn circuit breakers — SDK caps wired into options; wall-clock timeout
     discards the pooled client and surfaces a timeout error.
  #2 append-only session store — round-trip, ordering, prune-on-new-session,
     legacy-blob fallback, subagent skip.
  #3 per-repo git lock — mutating actions serialize per repo_path but run
     concurrently across different repos.
  #4 bounded LRU caches — eviction past the cap, recently-used survives.
  #5 consolidated deny rules — one source of truth, shared by brain + dev.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentic.config import settings


# ============================================================================
# #1 — per-turn circuit breakers
# ============================================================================


def test_loop_caps_present_by_default():
    from agentic.sdk.brain_session import _loop_caps

    caps = _loop_caps()
    assert caps["max_turns"] == settings.brain_max_turns
    assert caps["max_budget_usd"] == settings.brain_max_budget_usd


def test_loop_caps_omitted_when_zero(monkeypatch):
    from agentic.sdk.brain_session import _loop_caps

    monkeypatch.setattr(settings, "brain_max_turns", 0)
    monkeypatch.setattr(settings, "brain_max_budget_usd", 0.0)
    assert _loop_caps() == {}


async def test_brain_options_wire_loop_caps():
    from agentic.sdk import PendingPermissions
    from agentic.sdk.brain_session import make_brain_options_factory
    from agentic.store import init_db, touch_thread

    init_db()
    thread_ts = "1700000000.caps01"
    touch_thread(thread_ts, "C_CAPS")
    factory = make_brain_options_factory(
        pending=PendingPermissions(), session_store=None, slack_client=AsyncMock()
    )
    opts = await factory(thread_ts)
    assert opts.max_turns == settings.brain_max_turns
    assert opts.max_budget_usd == settings.brain_max_budget_usd


class _HangingClient:
    """A pooled client whose receive stream never terminates."""

    async def query(self, *_a, **_k):
        return None

    async def receive_response(self):
        await asyncio.sleep(60)
        yield  # pragma: no cover — never reached


class _FakePool:
    def __init__(self, client):
        self._client = client
        self.released: list[str] = []

    async def get_or_create(self, thread_ts):
        return self._client

    async def release(self, thread_ts):
        self.released.append(thread_ts)


class _ResultClient:
    """A pooled client that streams a single terminal ResultMessage."""

    def __init__(self, result_msg):
        self._result_msg = result_msg

    async def query(self, *_a, **_k):
        return None

    async def receive_response(self):
        yield self._result_msg


async def test_cap_hit_surfaces_human_message(monkeypatch):
    """When a per-turn cap fires, the SDK flags is_error with a raw stop_reason
    (tool_use / end_turn). run_brain_session must translate that into a human note,
    not leak 'tool_use' to the user."""
    from claude_agent_sdk import ResultMessage

    from agentic.sdk import PendingPermissions
    from agentic.sdk import brain_session as bs

    monkeypatch.setattr(settings, "brain_timeout_s", 30)
    for stop in ("tool_use", "end_turn", "max_tokens"):
        rm = ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=10,
            is_error=True, num_turns=3, session_id="s1", stop_reason=stop,
            total_cost_usd=0.01, result="",
        )
        pool = _FakePool(_ResultClient(rm))
        result = await bs.run_brain_session(
            user_text="x", thread_ts=f"T_{stop}", channel_id="C1",
            slack_client=AsyncMock(), placeholder_ts="1.1", thread_history=[],
            workspace_hint=None, pool=pool, pending=PendingPermissions(),
        )
        assert result.error and "giới hạn an toàn" in result.error, (stop, result.error)
        assert stop not in result.error  # raw reason must not leak


async def test_genuine_result_error_still_surfaced(monkeypatch):
    """A real error stop_reason is passed through, not masked as a cap message."""
    from claude_agent_sdk import ResultMessage

    from agentic.sdk import PendingPermissions
    from agentic.sdk import brain_session as bs

    monkeypatch.setattr(settings, "brain_timeout_s", 30)
    rm = ResultMessage(
        subtype="error", duration_ms=10, duration_api_ms=10, is_error=True,
        num_turns=1, session_id="s1", stop_reason="refusal", result="model từ chối",
    )
    pool = _FakePool(_ResultClient(rm))
    result = await bs.run_brain_session(
        user_text="x", thread_ts="T_err", channel_id="C1", slack_client=AsyncMock(),
        placeholder_ts="1.1", thread_history=[], workspace_hint=None,
        pool=pool, pending=PendingPermissions(),
    )
    assert result.error == "model từ chối"
    assert "giới hạn an toàn" not in result.error


async def test_brain_session_timeout_releases_client(monkeypatch):
    from agentic.sdk import PendingPermissions
    from agentic.sdk import brain_session as bs

    monkeypatch.setattr(settings, "brain_timeout_s", 0.05)
    pool = _FakePool(_HangingClient())
    result = await bs.run_brain_session(
        user_text="hi",
        thread_ts="T_TIMEOUT",
        channel_id="C1",
        slack_client=AsyncMock(),
        placeholder_ts="123.45",
        thread_history=[],
        workspace_hint=None,
        pool=pool,
        pending=PendingPermissions(),
    )
    assert result.error and "hết thời gian" in result.error
    # The half-consumed client must be discarded so the next turn reopens clean.
    assert pool.released == ["T_TIMEOUT"]


# ============================================================================
# #2 — append-only session store
# ============================================================================


def test_session_entries_round_trip_and_order():
    from agentic.store import (
        append_session_entries,
        load_session_entries,
        init_db,
        touch_thread,
    )

    init_db()
    tts, sid = "T_SESS1", "sess-A"
    touch_thread(tts, "C")
    append_session_entries(tts, sid, ['{"i":1}', '{"i":2}'])
    append_session_entries(tts, sid, ['{"i":3}'])
    assert load_session_entries(tts, sid) == ['{"i":1}', '{"i":2}', '{"i":3}']


def test_session_entries_prune_on_new_session():
    from agentic.store import (
        append_session_entries,
        get_thread,
        load_session_entries,
        init_db,
        touch_thread,
    )

    init_db()
    tts = "T_SESS2"
    touch_thread(tts, "C")
    append_session_entries(tts, "old", ['{"x":1}'])
    append_session_entries(tts, "new", ['{"y":1}'])
    # Old session's rows pruned; only the live session remains.
    assert load_session_entries(tts, "old") == []
    assert load_session_entries(tts, "new") == ['{"y":1}']
    # Resume token tracks the live session.
    assert get_thread(tts)["sdk_session_id"] == "new"


def test_session_entries_legacy_blob_fallback():
    from agentic.store import connect, load_session_entries, init_db, touch_thread

    init_db()
    tts, sid = "T_SESS3", "legacy-sess"
    touch_thread(tts, "C")
    with connect() as conn:
        conn.execute(
            "UPDATE threads SET sdk_session_id=?, sdk_state_blob=? WHERE thread_ts=?",
            (sid, '[{"k":1},{"k":2}]', tts),
        )
    # No session_entries rows → read the pre-migration blob, one entry per element.
    assert load_session_entries(tts, sid) == ['{"k": 1}', '{"k": 2}']


async def test_session_store_adapter_round_trip():
    from agentic.sdk.session_store import SqliteSessionStore
    from agentic.store import init_db, touch_thread

    init_db()
    tts, sid = "T_SESS4", "adapter-sess"
    touch_thread(tts, "C")
    store = SqliteSessionStore()
    key = {"project_key": tts, "session_id": sid, "subpath": None}
    await store.append(key, [{"role": "user"}, {"role": "assistant"}])
    assert await store.load(key) == [{"role": "user"}, {"role": "assistant"}]
    # Subagent transcripts (subpath set) are skipped both ways.
    sub = {"project_key": tts, "session_id": sid, "subpath": "dev"}
    await store.append(sub, [{"role": "x"}])
    assert await store.load(sub) is None


# ============================================================================
# #3 — per-repo git lock
# ============================================================================


async def test_git_mutating_actions_serialize_per_repo(monkeypatch):
    from agentic.integrations import git as g
    from agentic.integrations.result import ToolResult

    monkeypatch.setattr(g, "_repo_path_for_action", lambda at, p: "/repo/same")
    state = {"active": 0, "max": 0}

    async def slow(_payload):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.03)
        state["active"] -= 1
        return ToolResult.success("ok")

    monkeypatch.setitem(g.ACTION_HANDLERS, "git.push", lambda p: slow(p))
    await asyncio.gather(
        *[g.execute_action("git.push", {"service": "s", "ticket": "T-1"})
          for _ in range(5)]
    )
    assert state["max"] == 1  # never two at once on the same repo


async def test_git_mutating_actions_concurrent_across_repos(monkeypatch):
    from agentic.integrations import git as g
    from agentic.integrations.result import ToolResult

    counter = {"n": 0}

    def distinct_repo(_at, _p):
        counter["n"] += 1
        return f"/repo/{counter['n']}"

    monkeypatch.setattr(g, "_repo_path_for_action", distinct_repo)
    state = {"active": 0, "max": 0}

    async def slow(_payload):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.03)
        state["active"] -= 1
        return ToolResult.success("ok")

    monkeypatch.setitem(g.ACTION_HANDLERS, "git.push", lambda p: slow(p))
    await asyncio.gather(
        *[g.execute_action("git.push", {"service": "s", "ticket": "T-1"})
          for _ in range(4)]
    )
    assert state["max"] > 1  # different repos don't block each other


# ============================================================================
# #4 — bounded LRU caches
# ============================================================================


def test_user_cache_evicts_past_cap(monkeypatch):
    from agentic import slack_handlers as sh

    monkeypatch.setattr(sh, "_CACHE_MAX_ENTRIES", 3)
    sh._user_info_cache.clear()
    for i in range(5):
        sh._cache_put(sh._user_info_cache, f"U{i}", ("n", None, 0.0))
    assert len(sh._user_info_cache) == 3
    assert list(sh._user_info_cache) == ["U2", "U3", "U4"]  # oldest evicted


def test_cache_refresh_keeps_recently_used(monkeypatch):
    from agentic import slack_handlers as sh

    monkeypatch.setattr(sh, "_CACHE_MAX_ENTRIES", 3)
    sh._user_info_cache.clear()
    for i in range(3):
        sh._cache_put(sh._user_info_cache, f"U{i}", ("n", None, 0.0))
    sh._cache_put(sh._user_info_cache, "U0", ("n", None, 1.0))  # touch oldest
    sh._cache_put(sh._user_info_cache, "U3", ("n", None, 0.0))  # forces eviction
    assert "U0" in sh._user_info_cache  # survived — was refreshed
    assert "U1" not in sh._user_info_cache  # now the oldest, evicted


# ============================================================================
# #5 — consolidated deny rules
# ============================================================================


def test_deny_list_single_source_of_truth():
    from agentic.sdk import permission as perm
    from agentic.sdk import sub_agents as sa

    # sub_agents re-exports the gating module's list (same object).
    assert sa.DEV_DISALLOWED_TOOLS is perm.SESSION_DISALLOWED_TOOLS
    assert "Bash(git push --force:*)" in perm.SESSION_DISALLOWED_TOOLS
    # The dev AgentDefinition carries it.
    assert build_subagents_dev().disallowedTools is perm.SESSION_DISALLOWED_TOOLS


def build_subagents_dev():
    from agentic.sdk.sub_agents import build_subagents

    return build_subagents()["dev"]
