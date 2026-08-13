import os
import time

import httpx
import pytest

from agentic import slack_handlers as sh
from agentic.config import settings
from agentic.slack_handlers import (
    _ATTACH_MAX_CHARS,
    _IMAGE_MAX_BYTES,
    _save_image,
    _chunks,
    _fetch_thread_history,
    _markdown_block,
    _notify_text,
    _placeholder_for,
    _render_files,
)


def _served(payload: bytes):
    """Stand-in for the authenticated `url_private` GET (real HTTP is out of scope
    for the hermetic suite)."""

    async def _fake_download(client, f, *, max_bytes, partial_ok):
        if len(payload) > max_bytes and not partial_ok:
            return None, f"larger than {max_bytes // 1_000_000}MB"
        return payload[:max_bytes], ""

    return _fake_download


class _FakeClient:
    """files.info stub — snippets come back with `content` inline, so no HTTP."""

    def __init__(self, files: dict):
        self._files = files
        self.messages: list[dict] = []
        self.token = "xoxb-test"

    async def files_info(self, file: str):
        if file not in self._files:
            raise RuntimeError("file_not_found")
        return {"file": self._files[file]}

    async def conversations_replies(self, channel: str, ts: str, limit: int = 20):
        return {"messages": self.messages}


def test_markdown_block_passes_text_through_unchanged():
    # Slack converts standard GFM server-side, so we send the brain's markdown
    # verbatim — no local mrkdwn translation that previously dropped headings,
    # tables, lists, etc.
    text = "## Heading\n- **bold** item\n\n| a | b |\n|---|---|\n| 1 | 2 |"

    blocks = _markdown_block(text)

    assert blocks == [{"type": "markdown", "text": text}]


def test_notify_text_is_first_line_clipped():
    assert _notify_text("first line\nsecond line") == "first line"
    assert _notify_text("  \n  ") == "message"
    assert _notify_text("x" * 200) == "x" * 150


def test_chunks_prefers_paragraph_boundaries():
    text = "first paragraph\n\nsecond paragraph\n\nthird paragraph"

    assert _chunks(text, limit=30) == ["first paragraph", "second paragraph", "third paragraph"]


def test_fix_pr_gets_specific_placeholder():
    text = "fix 3 critical trong PR https://github.com/org/repo/pull/1"

    assert _placeholder_for(text) == "⏳ Preparing the PR worktree to fix..."


@pytest.mark.asyncio
async def test_snippet_content_is_inlined():
    sql = "UPDATE drivers SET name = 'x';"
    client = _FakeClient(
        {"F1": {"id": "F1", "name": "q.sql", "filetype": "sql", "content": sql}}
    )
    msg = {"files": [{"id": "F1", "name": "q.sql", "mimetype": "text/plain"}]}

    out, _ = await _render_files(client, msg, 5, 20_000)

    assert sql in out
    assert 'attachment "q.sql"' in out


@pytest.mark.asyncio
async def test_unreadable_attachment_is_announced_not_dropped():
    client = _FakeClient({})
    msg = {"files": [{"id": "MISSING", "name": "q.sql", "mimetype": "text/plain"}]}

    out, _ = await _render_files(client, msg, 5, 20_000)

    assert 'attachment "q.sql" could not be read' in out


@pytest.mark.asyncio
async def test_non_text_non_image_attachment_is_flagged_and_not_fetched():
    client = _FakeClient({"F2": {"id": "F2", "name": "doc.pdf", "mimetype": "application/pdf"}})
    msg = {"files": [{"id": "F2", "name": "doc.pdf", "mimetype": "application/pdf"}]}

    out, _ = await _render_files(client, msg, 5, 20_000)

    assert out == '[attachment "doc.pdf" (application/pdf) — not text, not read]'


@pytest.mark.asyncio
async def test_image_is_saved_and_handed_to_the_brain_as_a_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
    client = _FakeClient(
        {"IMG": {"id": "IMG", "name": "shot.png", "mimetype": "image/png",
                 "url_private": "https://files.slack.com/shot.png"}}
    )
    monkeypatch.setattr(sh, "_download", _served(png))
    msg = {"files": [{"id": "IMG", "name": "shot.png", "mimetype": "image/png"}]}

    out, _ = await _render_files(client, msg, 5, 20_000)

    saved = list(tmp_path.iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == png
    assert str(saved[0]) in out
    assert "Read" in out


@pytest.mark.asyncio
async def test_oversized_image_is_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    client = _FakeClient(
        {"BIG": {"id": "BIG", "name": "huge.png", "mimetype": "image/png",
                 "size": _IMAGE_MAX_BYTES + 1, "url_private": "https://x/huge.png"}}
    )
    msg = {"files": [{"id": "BIG", "name": "huge.png", "mimetype": "image/png"}]}

    out, _ = await _render_files(client, msg, 5, 20_000)

    assert "too large" in out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_stale_attachments_are_pruned_on_save(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "attachment_dir", str(tmp_path))
    stale = tmp_path / "old-shot.png"
    stale.write_bytes(b"old")
    old_mtime = time.time() - (settings.attachment_ttl_h + 1) * 3600
    os.utime(stale, (old_mtime, old_mtime))

    _save_image("IMG", "new.png", b"new")

    assert not stale.exists()
    assert (tmp_path / "IMG-new.png").exists()


@pytest.mark.asyncio
async def test_long_attachment_is_truncated():
    client = _FakeClient(
        {"F3": {"id": "F3", "name": "big.log", "content": "x" * (_ATTACH_MAX_CHARS + 500)}}
    )
    msg = {"files": [{"id": "F3", "name": "big.log", "mimetype": "text/plain"}]}

    out, _ = await _render_files(client, msg, 5, 20_000)

    assert "…[attachment truncated at 20000 chars]" in out
    assert len(out) < _ATTACH_MAX_CHARS + 200


@pytest.mark.asyncio
async def test_attachments_are_budgeted_against_the_downstream_input_cap():
    # Two max-sized files would blow past dispatcher's max_input_chars, where the
    # tail file gets cut off wholesale rather than trimmed.
    client = _FakeClient(
        {f"F{i}": {"id": f"F{i}", "name": f"f{i}.log", "content": "x" * 9_000}
         for i in range(2)}
    )
    msg = {"files": [{"id": f"F{i}", "mimetype": "text/plain"} for i in range(2)]}

    out, _ = await _render_files(client, msg, 5, 10_000)

    assert len(out) < 11_000
    assert "truncated at" in out  # second file trimmed to what was left, not dropped


def _mock_http(monkeypatch, payload: bytes):
    """Point _download's httpx client at a canned response body."""

    real_client = httpx.AsyncClient  # captured before the patch, else infinite recursion

    def _factory(**kw):
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(
                200, content=payload, headers={"content-type": "text/plain"}
            )
        )
        return real_client(transport=transport)

    monkeypatch.setattr(sh.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_download_stops_at_the_byte_ceiling(monkeypatch):
    _mock_http(monkeypatch, b"y" * 5_000_000)

    raw, err = await sh._download(
        _FakeClient({}), {"url_private": "https://x/f"}, max_bytes=1000, partial_ok=True
    )

    assert err == ""
    assert len(raw) == 1000


@pytest.mark.asyncio
async def test_oversized_download_errors_when_partial_is_useless(monkeypatch):
    _mock_http(monkeypatch, b"y" * 5_000_000)

    raw, err = await sh._download(
        _FakeClient({}),
        {"url_private": "https://x/f"},
        max_bytes=1_000_000,
        partial_ok=False,
    )

    assert raw is None
    assert "larger than" in err


@pytest.mark.asyncio
async def test_attachment_is_announced_when_no_budget_remains():
    client = _FakeClient({"F1": {"id": "F1", "name": "q.sql", "content": "SELECT 1"}})
    msg = {"files": [{"id": "F1", "name": "q.sql", "mimetype": "text/plain"}]}

    out, _ = await _render_files(client, msg, 5, 10)

    assert out == '[attachment "q.sql" not included — no context budget left]'


@pytest.mark.asyncio
async def test_render_files_reports_only_the_files_it_consumed():
    client = _FakeClient(
        {f"F{i}": {"id": f"F{i}", "name": f"f{i}.sql", "content": "x"} for i in range(4)}
    )
    msg = {"files": [{"id": f"F{i}", "mimetype": "text/plain"} for i in range(4)]}

    out, used = await _render_files(client, msg, 2, 20_000)

    assert used == 2
    assert out.count("attachment") == 2


@pytest.mark.asyncio
async def test_history_spends_the_file_budget_on_the_newest_message():
    client = _FakeClient({"NEW": {"id": "NEW", "name": "new.sql", "content": "SELECT 1"}})
    client.messages = [
        {"ts": "1.0", "text": "old", "files": [{"id": "OLD", "mimetype": "text/plain"}]},
        {"ts": "2.0", "text": "new", "files": [{"id": "NEW", "mimetype": "text/plain"}]},
    ]

    history = await _fetch_thread_history(client, "C1", "1.0", "3.0", None)

    assert [h["text"].split("\n")[0] for h in history] == ["old", "new"]
    assert "SELECT 1" in history[1]["text"]  # newest message got the budget


@pytest.mark.asyncio
async def test_history_keeps_a_message_that_is_only_an_attachment():
    client = _FakeClient({"F1": {"id": "F1", "name": "q.sql", "content": "SELECT 2"}})
    client.messages = [{"ts": "1.0", "files": [{"id": "F1", "mimetype": "text/plain"}]}]

    history = await _fetch_thread_history(client, "C1", "1.0", "3.0", None)

    assert len(history) == 1
    assert "SELECT 2" in history[0]["text"]
