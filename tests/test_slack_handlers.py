from agentic.slack_handlers import _chunks, _placeholder_for, _to_slack_mrkdwn


def test_to_slack_mrkdwn_converts_common_github_markdown():
    text = "**[review]**\n### ⛔ Blocking issues\n- **[critical]** `file.go:1` — bug"

    out = _to_slack_mrkdwn(text)

    assert out == "*[review]*\n⛔ Blocking issues\n- *[critical]* `file.go:1` — bug"


def test_to_slack_mrkdwn_renders_table_as_code_block():
    text = (
        "Verdict:\n"
        "| Claim | Status | Evidence |\n"
        "|---|---|---|\n"
        "| `LastStartAt` fence | **✅ ACCEPT** | command_repo.go |\n"
        "trailing line"
    )

    out = _to_slack_mrkdwn(text)
    lines = out.splitlines()

    assert lines[0] == "Verdict:"
    assert lines[1] == "```"
    # separator row dropped; header + body aligned, inline md flattened
    assert lines[2] == "Claim             | Status   | Evidence"
    assert lines[3] == "LastStartAt fence | ✅ ACCEPT | command_repo.go"
    assert lines[4] == "```"
    assert lines[5] == "trailing line"


def test_to_slack_mrkdwn_converts_links():
    out = _to_slack_mrkdwn("see [PR #1](https://github.com/o/r/pull/1) now")

    assert out == "see <https://github.com/o/r/pull/1|PR #1> now"


def test_chunks_prefers_paragraph_boundaries():
    text = "first paragraph\n\nsecond paragraph\n\nthird paragraph"

    assert _chunks(text, limit=30) == ["first paragraph", "second paragraph", "third paragraph"]


def test_fix_pr_gets_specific_placeholder():
    text = "fix 3 critical trong PR https://github.com/org/repo/pull/1"

    assert _placeholder_for(text) == "⏳ Đang chuẩn bị PR worktree để fix..."
