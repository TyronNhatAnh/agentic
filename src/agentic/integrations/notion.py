"""Notion connector — publish docs/notes to Notion.

V1 surface is intentionally tiny: create a page (with a markdown body) under a
parent page. That backs the ``notion_create_page`` MCP tool the brain can call.

We do NOT pull in a markdown library — the converter handles the common block
kinds (headings h1-h3, bullets, numbered items, fenced code, paragraphs).
Anything fancier degrades to a paragraph rather than failing. Notion limits we
respect: ≤100 block children per request (extra
blocks are PATCH-appended), ≤2000 chars per rich_text segment (long text is split
across segments within one block).
"""

from __future__ import annotations

import re

import httpx

from ..config import settings
from .result import ToolResult

_API = "https://api.notion.com/v1"
_RT_LIMIT = 2000          # max chars per rich_text segment
_CHILDREN_LIMIT = 100     # max block children per create/append request


def _headers() -> dict[str, str]:
    if not settings.notion_token:
        raise RuntimeError("NOTION_TOKEN chưa cấu hình")
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": settings.notion_version,
        "Content-Type": "application/json",
    }


def _rich_text(text: str) -> list[dict]:
    """Split text into ≤2000-char rich_text segments (Notion's per-segment cap)."""
    text = text or ""
    if not text:
        return [{"type": "text", "text": {"content": ""}}]
    return [
        {"type": "text", "text": {"content": text[i : i + _RT_LIMIT]}}
        for i in range(0, len(text), _RT_LIMIT)
    ]


def _block(kind: str, text: str, **extra) -> dict:
    payload = {"rich_text": _rich_text(text), **extra}
    return {"type": kind, kind: payload}


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")


def markdown_to_blocks(md: str) -> list[dict]:
    """Best-effort markdown → Notion block list. Unknown constructs become
    paragraphs; consecutive plain lines are merged into one paragraph."""
    blocks: list[dict] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(_block("paragraph", "\n".join(para)))
            para.clear()

    lines = (md or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block.
        if stripped.startswith("```"):
            flush_para()
            lang = stripped[3:].strip() or "plain text"
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append(_block("code", "\n".join(body), language=lang))
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        m = _HEADING_RE.match(stripped)
        if m:
            flush_para()
            level = min(len(m.group(1)), 3)
            blocks.append(_block(f"heading_{level}", m.group(2).strip()))
            i += 1
            continue

        m = _BULLET_RE.match(stripped)
        if m:
            flush_para()
            blocks.append(_block("bulleted_list_item", m.group(1).strip()))
            i += 1
            continue

        m = _NUMBERED_RE.match(stripped)
        if m:
            flush_para()
            blocks.append(_block("numbered_list_item", m.group(1).strip()))
            i += 1
            continue

        para.append(line.rstrip())
        i += 1

    flush_para()
    return blocks


# --- Reading: page id extraction + block → markdown ------------------------

# Matches a 32-hex Notion id, dashed (UUID) or bare. Notion URLs end with the
# bare form; the API accepts either.
_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


def _extract_page_id(ref: str) -> str:
    """Accept a Notion page id (dashed or bare) or a page URL and return the
    dashed UUID the API wants. Picks the LAST id-looking run — in a URL the id is
    the trailing segment, after the human-readable slug."""
    ref = (ref or "").strip()
    matches = _ID_RE.findall(ref)
    if not matches:
        raise ValueError(f"không tìm thấy Notion page id trong: {ref!r}")
    h = matches[-1].replace("-", "").lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _plain(content: dict) -> str:
    return "".join(seg.get("plain_text", "") for seg in (content.get("rich_text") or []))


def _page_title(page: dict) -> str:
    """The title property has a caller-defined name (``title`` for page-parent
    pages, e.g. ``Name`` for database rows) — find it by type, not by key."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(seg.get("plain_text", "") for seg in prop.get("title", []))
    return ""


def _block_to_md(block: dict, indent: int = 0) -> list[str]:
    """Render one Notion block as markdown line(s). Best-effort inverse of
    ``markdown_to_blocks`` plus a few read-only kinds (to_do, quote, callout,
    toggle, divider, child_page). Unknown kinds fall back to their plain text."""
    t = block.get("type", "")
    c = block.get(t, {}) if t else {}
    pad = "  " * indent
    if t == "heading_1":
        return [f"# {_plain(c)}"]
    if t == "heading_2":
        return [f"## {_plain(c)}"]
    if t == "heading_3":
        return [f"### {_plain(c)}"]
    if t == "bulleted_list_item" or t == "toggle":
        return [f"{pad}- {_plain(c)}"]
    if t == "numbered_list_item":
        return [f"{pad}1. {_plain(c)}"]
    if t == "to_do":
        return [f"{pad}- [{'x' if c.get('checked') else ' '}] {_plain(c)}"]
    if t == "quote" or t == "callout":
        return [f"> {_plain(c)}"]
    if t == "code":
        return [f"```{c.get('language', '')}\n{_plain(c)}\n```"]
    if t == "divider":
        return ["---"]
    if t == "child_page":
        return [f"## {c.get('title', '')} (child page)"]
    txt = _plain(c)
    return [f"{pad}{txt}"] if txt else [""]


async def create_page(
    title: str, markdown: str = "", parent_id: str | None = None
) -> ToolResult:
    """Create a Notion page (title + markdown body) under ``parent_id`` (defaults
    to ``NOTION_PARENT_PAGE_ID``). Returns the page URL on success."""
    parent_id = (parent_id or settings.notion_parent_page_id or "").strip()
    if not parent_id:
        raise RuntimeError("NOTION_PARENT_PAGE_ID chưa cấu hình (và không truyền parent)")
    parent_id = _extract_page_id(parent_id)  # tolerate a pasted URL / bare id
    if not (title or "").strip():
        raise ValueError("title rỗng")

    blocks = markdown_to_blocks(markdown)
    body = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
        },
        "children": blocks[:_CHILDREN_LIMIT],
    }

    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        resp = await client.post(f"{_API}/pages", json=body)
        resp.raise_for_status()
        page = resp.json()
        page_id = page.get("id", "")
        url = page.get("url", "")

        # Append any blocks beyond the first 100, in ≤100 batches.
        rest = blocks[_CHILDREN_LIMIT:]
        for start in range(0, len(rest), _CHILDREN_LIMIT):
            batch = rest[start : start + _CHILDREN_LIMIT]
            r = await client.patch(
                f"{_API}/blocks/{page_id}/children", json={"children": batch}
            )
            r.raise_for_status()

    return ToolResult.success(
        {"message": f"Đã tạo trang Notion: {url}", "url": url, "id": page_id}
    )


async def _read_children(client: httpx.AsyncClient, block_id: str, depth: int, max_depth: int) -> list[str]:
    """Fetch a block's children (paginated) and render to markdown lines,
    recursing into nested blocks up to ``max_depth`` so toggles/nested lists show."""
    lines: list[str] = []
    cursor: str | None = None
    while True:
        params = {"page_size": _CHILDREN_LIMIT}
        if cursor:
            params["start_cursor"] = cursor
        r = await client.get(f"{_API}/blocks/{block_id}/children", params=params)
        r.raise_for_status()
        data = r.json()
        for blk in data.get("results", []):
            lines.extend(_block_to_md(blk, depth))
            if blk.get("has_children") and depth < max_depth:
                lines.extend(await _read_children(client, blk["id"], depth + 1, max_depth))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return lines


def _page_read_result(page_id: str, title: str, url: str, body: str) -> ToolResult:
    """Build the read ToolResult. Crucially sets ``message`` — ToolResult.display()
    (what the MCP layer sends the brain) renders ONLY data["message"] for dict
    payloads, so omitting it makes a successful read surface as empty text."""
    header = f"# {title}\n<{url}>".strip()
    body = (body or "").strip()
    message = f"{header}\n\n{body}" if body else f"{header}\n\n_(page không có nội dung block)_"
    return ToolResult.success(
        {"message": message, "title": title, "url": url, "id": page_id, "markdown": body}
    )


async def get_page(ref: str, max_depth: int = 4) -> ToolResult:
    """Read a Notion page (id or URL) → title + URL + markdown body. Requires the
    integration behind NOTION_TOKEN to be shared on the page, else Notion 404s."""
    page_id = _extract_page_id(ref)
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        pr = await client.get(f"{_API}/pages/{page_id}")
        pr.raise_for_status()
        page = pr.json()
        lines = await _read_children(client, page_id, 0, max_depth)
    return _page_read_result(
        page_id, _page_title(page), page.get("url", ""), "\n".join(lines)
    )


async def _clear_children(client: httpx.AsyncClient, block_id: str) -> int:
    """Archive every direct child block of a page (Notion has no bulk replace —
    delete = archive). Returns the count removed."""
    ids: list[str] = []
    cursor: str | None = None
    while True:
        params = {"page_size": _CHILDREN_LIMIT}
        if cursor:
            params["start_cursor"] = cursor
        r = await client.get(f"{_API}/blocks/{block_id}/children", params=params)
        r.raise_for_status()
        data = r.json()
        ids.extend(b["id"] for b in data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    for bid in ids:
        r = await client.delete(f"{_API}/blocks/{bid}")
        r.raise_for_status()
    return len(ids)


async def _append_blocks(client: httpx.AsyncClient, page_id: str, markdown: str) -> None:
    blocks = markdown_to_blocks(markdown)
    for start in range(0, len(blocks), _CHILDREN_LIMIT):
        batch = blocks[start : start + _CHILDREN_LIMIT]
        r = await client.patch(f"{_API}/blocks/{page_id}/children", json={"children": batch})
        r.raise_for_status()


async def update_page(
    ref: str,
    title: str | None = None,
    markdown: str | None = None,
    replace_body: bool = False,
) -> ToolResult:
    """Update a Notion page: rename (``title``) and/or write body (``markdown``).
    ``replace_body=True`` archives existing content first (full rewrite); else the
    markdown is appended. At least one of title/markdown must be given."""
    if not (title or "").strip() and not (markdown or "").strip():
        raise ValueError("cần ít nhất title hoặc markdown để cập nhật")
    page_id = _extract_page_id(ref)
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        pr = await client.get(f"{_API}/pages/{page_id}")
        pr.raise_for_status()
        page = pr.json()
        if (title or "").strip():
            # Patch the title property under its actual name (see _page_title).
            title_key = next(
                (k for k, v in (page.get("properties") or {}).items() if v.get("type") == "title"),
                "title",
            )
            r = await client.patch(
                f"{_API}/pages/{page_id}",
                json={"properties": {title_key: {"title": [{"type": "text", "text": {"content": title[:2000]}}]}}},
            )
            r.raise_for_status()
        if (markdown or "").strip():
            if replace_body:
                await _clear_children(client, page_id)
            await _append_blocks(client, page_id, markdown)
        pr = await client.get(f"{_API}/pages/{page_id}")
        pr.raise_for_status()
        url = pr.json().get("url", "")
    return ToolResult.success(
        {"message": f"Đã cập nhật trang Notion: {url}", "url": url, "id": page_id}
    )


async def archive_page(ref: str, restore: bool = False) -> ToolResult:
    """Delete (archive) a Notion page, or restore it with ``restore=True``.
    Reversible — Notion archives rather than hard-deletes."""
    page_id = _extract_page_id(ref)
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        r = await client.patch(f"{_API}/pages/{page_id}", json={"archived": not restore})
        r.raise_for_status()
        url = r.json().get("url", "")
    verb = "khôi phục" if restore else "xoá (archive)"
    return ToolResult.success(
        {"message": f"Đã {verb} trang Notion: {url}", "url": url, "id": page_id, "archived": not restore}
    )


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    """Legacy-style dispatcher parity (mirrors other integrations)."""
    if action_type == "notion.create_page":
        return await create_page(
            payload["title"], payload.get("markdown", ""), payload.get("parent_id")
        )
    if action_type == "notion.get_page":
        return await get_page(payload["page"])
    if action_type == "notion.update_page":
        return await update_page(
            payload["page"],
            payload.get("title"),
            payload.get("markdown"),
            bool(payload.get("replace_body", False)),
        )
    if action_type == "notion.delete_page":
        return await archive_page(payload["page"], bool(payload.get("restore", False)))
    return ToolResult.failure("VALIDATION", f"notion action không hỗ trợ: {action_type}")
