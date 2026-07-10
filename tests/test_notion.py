"""Hermetic tests for the Notion connector's pure helpers — no network.

Covers page-id extraction (URL / bare / dashed) and block → markdown rendering.
The HTTP paths (get/create/update/archive) are not exercised here; they need a
live token + a shared page.
"""

import pytest

from agentic.integrations import notion


class TestExtractPageId:
    def test_from_url(self):
        url = "https://app.notion.com/p/gogox/Price-CD-Overview-List-97819da7a5ed40a7af8e169df44648c2"
        assert notion._extract_page_id(url) == "97819da7-a5ed-40a7-af8e-169df44648c2"

    def test_from_bare_id(self):
        assert notion._extract_page_id("97819da7a5ed40a7af8e169df44648c2") == \
            "97819da7-a5ed-40a7-af8e-169df44648c2"

    def test_from_dashed_uuid(self):
        dashed = "97819da7-a5ed-40a7-af8e-169df44648c2"
        assert notion._extract_page_id(dashed) == dashed

    def test_picks_trailing_id_not_slug(self):
        # A slug word ("decade") is hex-ish but too short; the trailing id wins.
        url = "https://notion.so/team/decade-report-0123456789abcdef0123456789abcdef"
        assert notion._extract_page_id(url) == "01234567-89ab-cdef-0123-456789abcdef"

    def test_no_id_raises(self):
        with pytest.raises(ValueError):
            notion._extract_page_id("https://notion.so/no-id-here")


class TestBlockToMd:
    def _rt(self, text):
        return {"rich_text": [{"plain_text": text}]}

    def test_headings(self):
        assert notion._block_to_md({"type": "heading_1", "heading_1": self._rt("H1")}) == ["# H1"]
        assert notion._block_to_md({"type": "heading_2", "heading_2": self._rt("H2")}) == ["## H2"]
        assert notion._block_to_md({"type": "heading_3", "heading_3": self._rt("H3")}) == ["### H3"]

    def test_bullet_and_numbered_indent(self):
        blk = {"type": "bulleted_list_item", "bulleted_list_item": self._rt("item")}
        assert notion._block_to_md(blk, indent=0) == ["- item"]
        assert notion._block_to_md(blk, indent=2) == ["    - item"]
        num = {"type": "numbered_list_item", "numbered_list_item": self._rt("one")}
        assert notion._block_to_md(num) == ["1. one"]

    def test_todo_checked_unchecked(self):
        base = self._rt("task")
        assert notion._block_to_md({"type": "to_do", "to_do": {**base, "checked": True}}) == ["- [x] task"]
        assert notion._block_to_md({"type": "to_do", "to_do": {**base, "checked": False}}) == ["- [ ] task"]

    def test_code_keeps_language(self):
        blk = {"type": "code", "code": {**self._rt("print(1)"), "language": "python"}}
        assert notion._block_to_md(blk) == ["```python\nprint(1)\n```"]

    def test_divider_and_unknown_fallback(self):
        assert notion._block_to_md({"type": "divider", "divider": {}}) == ["---"]
        # Unknown kind with text → falls back to plain text.
        assert notion._block_to_md({"type": "equation", "equation": self._rt("x=1")}) == ["x=1"]


class TestPageReadResult:
    """Regression: get_page must set data['message'] — ToolResult.display() renders
    only that for dict payloads, so a missing message surfaces a read as empty text
    to the brain (the bug that made the bot report 'tool trả rỗng')."""

    def test_message_present_and_display_nonempty(self):
        res = notion._page_read_result("id1", "My Page", "https://n/p/id1", "# H\nbody")
        assert res.ok
        assert res.data["message"]  # non-empty
        assert res.display()  # what the MCP layer sends the brain — must not be ""
        assert "My Page" in res.display()
        assert "body" in res.display()

    def test_empty_body_still_reports_something(self):
        res = notion._page_read_result("id1", "Empty", "https://n/p/id1", "   ")
        assert res.display().strip()  # not blank
        assert res.data["markdown"] == ""


def test_markdown_roundtrip_headings_bullets_code():
    md = "# Title\n- a\n- b\n\n```py\nx=1\n```"
    blocks = notion.markdown_to_blocks(md)
    kinds = [b["type"] for b in blocks]
    assert kinds == ["heading_1", "bulleted_list_item", "bulleted_list_item", "code"]
