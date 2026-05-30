from agentic.slack_handlers import _chunks, _placeholder_for, _to_slack_mrkdwn


def test_to_slack_mrkdwn_converts_common_github_markdown():
    text = "**[review]**\n### ⛔ Blocking issues\n- **[critical]** `file.go:1` — bug"

    out = _to_slack_mrkdwn(text)

    assert out == "*[review]*\n⛔ Blocking issues\n- *[critical]* `file.go:1` — bug"


def test_chunks_prefers_paragraph_boundaries():
    text = "first paragraph\n\nsecond paragraph\n\nthird paragraph"

    assert _chunks(text, limit=30) == ["first paragraph", "second paragraph", "third paragraph"]


def test_fix_pr_gets_specific_placeholder():
    text = "fix 3 critical trong PR https://github.com/org/repo/pull/1"

    assert _placeholder_for(text) == "⏳ Đang chuẩn bị PR worktree để fix..."
