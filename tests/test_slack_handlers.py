from agentic.slack_handlers import (
    _chunks,
    _markdown_block,
    _notify_text,
    _placeholder_for,
)


def test_markdown_block_passes_text_through_unchanged():
    # Slack converts standard GFM server-side, so we send the brain's markdown
    # verbatim — no local mrkdwn translation that previously dropped headings,
    # tables, lists, etc.
    text = "## Heading\n- **bold** item\n\n| a | b |\n|---|---|\n| 1 | 2 |"

    blocks = _markdown_block(text)

    assert blocks == [{"type": "markdown", "text": text}]


def test_notify_text_is_first_line_clipped():
    assert _notify_text("first line\nsecond line") == "first line"
    assert _notify_text("  \n  ") == "tin nhắn"
    assert _notify_text("x" * 200) == "x" * 150


def test_chunks_prefers_paragraph_boundaries():
    text = "first paragraph\n\nsecond paragraph\n\nthird paragraph"

    assert _chunks(text, limit=30) == ["first paragraph", "second paragraph", "third paragraph"]


def test_fix_pr_gets_specific_placeholder():
    text = "fix 3 critical trong PR https://github.com/org/repo/pull/1"

    assert _placeholder_for(text) == "⏳ Đang chuẩn bị PR worktree để fix..."
