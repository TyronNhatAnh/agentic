"""Phase 4 hermetic tests — hooks (§12.J) + observability columns (§12.K).

Asserts:
- build_brain_hooks registers the four hook events
- Pre/Post tool hooks write a runs row with the right agent/status/duration
- PostToolUseFailure logs an error row
- Secret tokens are redacted from the logged tool-input preview
- log_run persists the usage/cost/num_turns columns; tool rows leave them null
"""

from __future__ import annotations

import pytest

from agentic import store
from agentic.sdk.hooks import _redact, build_brain_hooks
from agentic.store import init_db, log_run


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store.settings, "agentic_db", str(tmp_path / "t.db"))
    store._PRAGMAS_APPLIED = False
    init_db()


def _rows():
    with store.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


def test_build_brain_hooks_registers_events():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    assert set(hooks) == {
        "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact"
    }
    # Each event carries a single HookMatcher with one callback.
    for matchers in hooks.values():
        assert len(matchers) == 1
        assert len(matchers[0].hooks) == 1


async def test_pre_post_tool_logs_ok_row_with_duration():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    pre = hooks["PreToolUse"][0].hooks[0]
    post = hooks["PostToolUse"][0].hooks[0]

    inp = {
        "tool_name": "github_get_pr",
        "tool_input": {"pr": 7},
        "tool_use_id": "tu_1",
    }
    await pre(inp, "tu_1", None)
    await post(inp, "tu_1", None)

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["agent"] == "github_get_pr"
    assert rows[0]["status"] == "ok"
    assert rows[0]["thread_ts"] == "t1"
    assert rows[0]["channel"] == "C1"
    assert rows[0]["duration_ms"] >= 0
    # Tool rows leave the observability columns null.
    assert rows[0]["cost_usd"] is None
    assert rows[0]["input_tokens"] is None


async def test_post_tool_logs_redacted_response_output():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    post = hooks["PostToolUse"][0].hooks[0]
    await post(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn x ."},
            "tool_response": {"content": [{"type": "text", "text": "no matches found"}]},
            "tool_use_id": "tu_o",
        },
        "tu_o",
        None,
    )
    row = _rows()[0]
    assert row["status"] == "ok"
    assert "no matches found" in row["output"]


async def test_post_tool_output_redacts_secrets():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    post = hooks["PostToolUse"][0].hooks[0]
    await post(
        {
            "tool_name": "git_push",
            "tool_input": {"cmd": "push"},
            "tool_response": "remote: https://x-access-token:ghp_AAAAAAAAAAAAAAAAAAAA@github.com/o/r.git",
            "tool_use_id": "tu_os",
        },
        "tu_os",
        None,
    )
    out = _rows()[0]["output"]
    assert "ghp_AAAAAAAAAAAAAAAAAAAA" not in out
    assert "«redacted»" in out


async def test_post_tool_failure_logs_error_row():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    fail = hooks["PostToolUseFailure"][0].hooks[0]
    await fail(
        {"tool_name": "jira_get_issue", "tool_input": {"key": "X-1"}, "error": "NOT_FOUND"},
        "tu_2",
        None,
    )
    rows = _rows()
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "NOT_FOUND"


async def test_tool_input_secrets_redacted_in_log():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    post = hooks["PostToolUse"][0].hooks[0]
    await post(
        {
            "tool_name": "git_push",
            "tool_input": {"url": "https://x-access-token:ghp_AAAAAAAAAAAAAAAAAAAA@github.com/o/r.git"},
            "tool_use_id": "tu_3",
        },
        "tu_3",
        None,
    )
    logged_input = _rows()[0]["input"]
    assert "ghp_AAAAAAAAAAAAAAAAAAAA" not in logged_input
    assert "«redacted»" in logged_input


def test_redact_patterns():
    assert "«redacted»" in _redact("ghp_0123456789ABCDEFghij")
    assert "«redacted»" in _redact("token xoxb-123456789012-abcd")
    assert _redact("plain text") == "plain text"


def test_log_run_persists_usage_columns():
    log_run(
        agent="brain",
        input_text="ping",
        output="pong",
        status="ok",
        duration_ms=100,
        thread_ts="t1",
        channel="C1",
        usage={
            "cache_read_input_tokens": 90,
            "cache_creation_input_tokens": 10,
            "input_tokens": 100,
            "output_tokens": 20,
        },
        cost_usd=0.05,
        num_turns=2,
    )
    row = _rows()[0]
    assert row["cache_read_input_tokens"] == 90
    assert row["cache_creation_input_tokens"] == 10
    assert row["output_tokens"] == 20
    assert row["cost_usd"] == 0.05
    assert row["num_turns"] == 2


async def test_pre_tool_denies_raw_net_git_in_bash():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    pre = hooks["PreToolUse"][0].hooks[0]

    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "cd /repo && git fetch origin --prune | tail -5"},
        "tool_use_id": "tu_git",
    }
    out = await pre(inp, "tu_git", None)
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "git_latest_release" in spec["permissionDecisionReason"]

    inp["tool_input"] = {"command": "git pull"}
    assert (await pre(inp, "tu_git", None))["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_pre_tool_allows_local_git_and_token_fetch():
    hooks = build_brain_hooks(thread_ts="t1", channel="C1")
    pre = hooks["PreToolUse"][0].hooks[0]

    for command in (
        "git log -1 --format='%H' origin/releases/DAPro-2.129",
        "git for-each-ref --sort=-committerdate 'refs/remotes/origin/releases/*'",
        'git -c http.extraheader="AUTHORIZATION: bearer $GITHUB_TOKEN" fetch https://github.com/gogovan/ggx-kr-da-api releases/DAPro-2.129',
        "git diff HEAD~1 | grep fetch",
    ):
        inp = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "tu_ok"}
        assert await pre(inp, "tu_ok", None) == {}

    # Non-Bash tools never match, whatever their input looks like.
    inp = {"tool_name": "Grep", "tool_input": {"pattern": "git fetch"}, "tool_use_id": "tu_g"}
    assert await pre(inp, "tu_g", None) == {}
