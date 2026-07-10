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


async def create_page(
    title: str, markdown: str = "", parent_id: str | None = None
) -> ToolResult:
    """Create a Notion page (title + markdown body) under ``parent_id`` (defaults
    to ``NOTION_PARENT_PAGE_ID``). Returns the page URL on success."""
    parent_id = (parent_id or settings.notion_parent_page_id or "").strip()
    if not parent_id:
        raise RuntimeError("NOTION_PARENT_PAGE_ID chưa cấu hình (và không truyền parent)")
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


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    """Legacy-style dispatcher parity (mirrors other integrations)."""
    if action_type == "notion.create_page":
        return await create_page(
            payload["title"], payload.get("markdown", ""), payload.get("parent_id")
        )
    return ToolResult.failure("VALIDATION", f"notion action không hỗ trợ: {action_type}")
