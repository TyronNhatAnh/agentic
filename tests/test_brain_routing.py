"""Thread-history rendering for the brain user message.

`_format_messages` moved into sdk/brain_session.py at the Phase 5 cutover (the
JSON-decision parser it used to live beside is gone — the brain emits native
tool_use blocks now, so there is nothing to parse)."""

from agentic.sdk.brain_session import _format_messages


def test_format_messages_keeps_useful_thread_history():
    review = "🔍 **Review: repo/service#431**\n\n### Blocking issues\n" + ("finding\n" * 100)

    out = _format_messages(
        [
            {"role": "user", "text": "review pr https://github.com/repo/service/pull/431"},
            {"role": "assistant", "text": review},
        ]
    )

    assert "repo/service#431" in out
    assert "Blocking issues" in out
    assert len(out) > 400


def test_format_messages_keeps_long_analysis_for_reuse():
    # A long in-thread analysis (the bot's own earlier breakdown) must survive
    # into the brain's view so it can be reused as a fix spec, not truncated away.
    analysis = "Lock assign order trong ggx-kr-da-api:\n" + ("chi tiết phân tích\n" * 300)
    assert len(analysis) > 4000  # well past the old 2400-char per-message cap

    out = _format_messages([{"role": "assistant", "text": analysis}])

    assert "ggx-kr-da-api" in out
    assert "message cắt bớt" not in out
    assert len(out) >= len(analysis)


def test_tool_label_strips_mcp_prefix():
    # MCP tools surface as mcp__agentic__<verb>; the progress line shows the verb
    # only. Native tools (Bash/Read/Task) pass through unchanged.
    from agentic.sdk.brain_session import _tool_label

    assert _tool_label("mcp__agentic__grafana_query_loki") == "grafana_query_loki"
    assert _tool_label("Bash") == "Bash"
    assert _tool_label("") == "tool"


def test_tool_progress_renders_only_after_first_tool():
    # Empty before any tool fires so the placeholder keeps its initial text until
    # there's something real to report; step count only once it's >1.
    from agentic.sdk.brain_session import _tool_progress

    assert _tool_progress("", 0) == ""
    assert _tool_progress("grafana_query_loki", 1) == "🔧 đang chạy `grafana_query_loki`"
    assert _tool_progress("Bash", 3) == "🔧 đang chạy `Bash` · 3 bước"


def test_session_cwd_is_stable_and_never_the_worktree(tmp_path, monkeypatch):
    # Regression: the bundled CLI keys resumable sessions by cwd. If cwd tracked
    # the mid-thread active_worktree, resume after idle eviction looked the session
    # up under a different project dir ("No conversation found" → exit 1 → the turn
    # crashed with "Command failed with exit code 1"). cwd must stay anchored to a
    # constant (workspace root here), with the worktree kept writable via add_dirs.
    from agentic.sdk import brain_session as bs
    from agentic.policy import PROD_POLICY

    worktree = tmp_path / "web-java" / "web-admin"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(bs.settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(bs.settings, "worktree_dir", "")

    cwd, add_dirs = bs._session_dirs({"active_worktree": str(worktree)}, PROD_POLICY)

    assert cwd == str(tmp_path)
    assert cwd != str(worktree)
    assert str(worktree) in add_dirs  # still writable under acceptEdits
