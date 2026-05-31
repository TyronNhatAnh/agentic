"""da-api revamp pipeline — SCAN → ARCHAEOLOGY → SPEC, all into Notion.

V1 deliberately stops before any Jira ticket or PR: the point is to get the
legacy analysis into Notion first for a human to review. Triggered by
``revamp <scope>`` in the revamp channel (dispatcher routes it here).

Why a Python orchestrator and not the brain: this is a deterministic, resumable
loop over modules, and — critically — each module is analysed in its **own**
one-shot ``ClaudeSDKClient`` so reading the legacy repo never bloats one shared
brain context (the single-session-per-thread limit). Per-module results are
persisted (``store.revamp_modules``) so a rerun skips work already published.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .agents.base import load_prompt
from .config import settings
from .integrations import notion as notion_int
from .store import (
    get_revamp_module,
    get_revamp_run,
    list_revamp_modules,
    upsert_revamp_module,
    upsert_revamp_run,
)

log = logging.getLogger(__name__)

# Bound on legacy markdown fed into the SPEC synthesis so a big run doesn't blow
# the spec agent's context. Each module contributes at most this many chars.
_SPEC_PER_MODULE_CHARS = 4000

_SPEC_SYSTEM_PROMPT = (
    "Bạn là kỹ sư senior tổng hợp tài liệu phân tích nhiều module legacy thành "
    "một bản SPEC sprint cho việc viết mới. Đầu vào là các phân tích module (mỗi "
    "phần có VERIFIED / HYPOTHESIS / MIGRATION PLAN). Trả về Markdown thuần gồm: "
    "tổng quan phạm vi, các epic đề xuất, danh sách story (Given/When/Then) gắn "
    "với module nguồn, thứ tự ưu tiên + rủi ro migration. Không bịa; chỗ nào dựa "
    "trên HYPOTHESIS thì ghi rõ. Không tạo ticket — chỉ tài liệu."
)


async def _run_oneshot(
    *,
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
    cwd: str | None,
    add_dirs: list[str] | None,
) -> str:
    """Run an isolated single-turn SDK query and return its concatenated text.

    Isolated on purpose: a fresh client per call means a module's file reads stay
    out of the interactive brain session and out of every other module's context.
    """
    opts = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode="default",
        model=settings.agent_model,
        cwd=cwd,
        add_dirs=add_dirs or [],
    )
    client = ClaudeSDKClient(options=opts)
    await client.connect()
    try:
        await client.query(user_prompt)
        parts: list[str] = []
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                break
        return "".join(parts).strip()
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.exception("revamp one-shot disconnect failed")


def _scan_modules(legacy_repo: str, scope: str) -> tuple[list[str], int]:
    """List analysable module units under ``legacy_repo/scope``.

    A unit is a direct child directory or ``.rb`` file of the scope path (one
    level). Returns (modules_relative_to_repo, dropped_count) where dropped_count
    is how many were cut by REVAMP_MODULE_CAP (surfaced, never silently dropped).
    """
    base = Path(legacy_repo)
    scope_path = base / scope
    if not scope_path.exists():
        raise FileNotFoundError(f"scope không tồn tại: {scope}")
    if scope_path.is_file():
        return [scope], 0
    units: list[str] = []
    for p in sorted(scope_path.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue
        if p.is_dir() or p.suffix == ".rb":
            units.append(str(Path(scope) / p.name))
    cap = settings.revamp_module_cap
    dropped = max(0, len(units) - cap)
    return units[:cap], dropped


async def _resolve_index_page(run_key: str, scope: str) -> tuple[str, str]:
    """Return (index_page_id, index_url) for this run, creating the index page on
    first run and reusing it on reruns so module pages keep nesting under it."""
    existing = get_revamp_run(run_key)
    if existing and existing.get("index_page_id"):
        return existing["index_page_id"], existing.get("index_url") or ""
    page = await notion_int.create_page(
        title=f"[revamp {scope}] INDEX",
        markdown=(
            f"Phân tích revamp cho scope `{scope}`.\n\n"
            "Các trang con bên dưới: 1 trang/module + 1 trang SPRINT SPEC. "
            "Trang này tự cập nhật danh sách con khi pipeline chạy thêm."
        ),
    )
    if not page.ok:
        raise RuntimeError(page.user_message or "tạo INDEX thất bại")
    index_id = (page.data or {}).get("id", "")
    index_url = (page.data or {}).get("url", "")
    if not index_id:
        raise RuntimeError("Notion không trả page id cho INDEX")
    upsert_revamp_run(run_key, index_id, index_url)
    return index_id, index_url


async def _progress(slack_client: Any, channel: str, ts: str, text: str) -> None:
    if not (slack_client and channel and ts):
        return
    try:
        await slack_client.chat_update(channel=channel, ts=ts, text=text)
    except Exception:
        log.debug("revamp progress update failed (non-fatal)")


async def run_revamp_pipeline(
    *,
    scope: str,
    thread_ts: str,
    channel: str,
    slack_client: Any,
    placeholder_ts: str | None,
) -> str:
    """Execute the revamp pipeline for ``scope`` and return a Slack summary."""
    legacy_repo = (settings.revamp_legacy_repo or "").strip()
    if not legacy_repo or not Path(legacy_repo).is_dir():
        return (
            "❌ REVAMP_LEGACY_REPO chưa cấu hình hoặc không phải thư mục: "
            f"`{legacy_repo or '(trống)'}`"
        )
    if not settings.notion_token or not settings.notion_parent_page_id:
        return (
            "❌ Cần NOTION_TOKEN và NOTION_PARENT_PAGE_ID để đẩy tài liệu lên Notion."
        )

    try:
        modules, dropped = _scan_modules(legacy_repo, scope)
    except FileNotFoundError as e:
        return f"❌ {e}"
    if not modules:
        return f"❌ Không tìm thấy module nào dưới scope `{scope}`."

    run_key = scope

    # Index page — module/spec pages nest under it so Notion shows one tidy tree
    # instead of N flat siblings. Reused across reruns of the same scope.
    try:
        index_id, index_url = await _resolve_index_page(run_key, scope)
    except Exception as e:  # noqa: BLE001
        log.exception("revamp index page failed scope=%s", scope)
        return f"❌ Không tạo được trang INDEX trên Notion: {e}"

    archaeologist_prompt = load_prompt("archaeologist")
    total = len(modules)
    fresh_docs: list[tuple[str, str]] = []  # (module, markdown) for SPEC input
    results: list[dict] = []  # {module, status, url, error}

    for idx, module in enumerate(modules, start=1):
        prior = get_revamp_module(run_key, module)
        if prior and prior.get("status") == "done" and prior.get("doc_url"):
            results.append({"module": module, "status": "skip", "url": prior["doc_url"]})
            continue

        await _progress(
            slack_client, channel, placeholder_ts,
            f"⏳ [revamp {scope}] phân tích module {idx}/{total}: `{module}`…",
        )
        try:
            markdown = await _run_oneshot(
                system_prompt=archaeologist_prompt,
                user_prompt=(
                    f"Phân tích module `{module}` trong repo này (đường dẫn tương đối "
                    "từ thư mục hiện tại). Đọc các file liên quan rồi trả về tài liệu "
                    "Markdown theo đúng cấu trúc đã hướng dẫn."
                ),
                allowed_tools=["Read", "Glob", "Grep"],
                cwd=legacy_repo,
                add_dirs=[legacy_repo],
            )
            if not markdown:
                raise RuntimeError("archaeologist trả về rỗng")
            page = await notion_int.create_page(
                title=f"[revamp {scope}] {module}", markdown=markdown,
                parent_id=index_id,
            )
            url = (page.data or {}).get("url", "") if page.ok else ""
            page_id = (page.data or {}).get("id", "") if page.ok else ""
            if not page.ok:
                raise RuntimeError(page.user_message or "tạo trang Notion thất bại")
            upsert_revamp_module(
                run_key, module, status="done", doc_url=url, doc_page_id=page_id
            )
            fresh_docs.append((module, markdown))
            results.append({"module": module, "status": "done", "url": url})
        except Exception as e:  # noqa: BLE001 — per-module boundary, keep going
            log.exception("revamp module failed module=%s", module)
            upsert_revamp_module(run_key, module, status="error", error=str(e))
            results.append({"module": module, "status": "error", "error": str(e)})

    # SPEC synthesis from the freshly produced docs (skipped modules already have
    # their pages; we only synthesise from what we analysed this run).
    spec_url = ""
    if fresh_docs:
        await _progress(
            slack_client, channel, placeholder_ts,
            f"⏳ [revamp {scope}] tổng hợp SPEC sprint…",
        )
        spec_input = "\n\n".join(
            f"## Module: {m}\n\n{md[:_SPEC_PER_MODULE_CHARS]}" for m, md in fresh_docs
        )
        try:
            spec_md = await _run_oneshot(
                system_prompt=_SPEC_SYSTEM_PROMPT,
                user_prompt=(
                    f"Scope: `{scope}`. Dưới đây là phân tích các module. Tổng hợp "
                    f"thành SPEC sprint:\n\n{spec_input}"
                ),
                allowed_tools=[],
                cwd=None,
                add_dirs=None,
            )
            if spec_md:
                spec_page = await notion_int.create_page(
                    title=f"[revamp {scope}] SPRINT SPEC", markdown=spec_md,
                    parent_id=index_id,
                )
                spec_url = (spec_page.data or {}).get("url", "") if spec_page.ok else ""
        except Exception as e:  # noqa: BLE001
            log.exception("revamp spec synthesis failed")
            spec_url = f"(SPEC lỗi: {e})"

    return _render_summary(scope, results, dropped, spec_url, index_url)


def _render_summary(
    scope: str, results: list[dict], dropped: int, spec_url: str, index_url: str = ""
) -> str:
    done = [r for r in results if r["status"] == "done"]
    skipped = [r for r in results if r["status"] == "skip"]
    errors = [r for r in results if r["status"] == "error"]

    lines = [f"*Revamp `{scope}` — xong*"]
    if index_url:
        lines.append(f"📁 INDEX (tất cả page nằm dưới đây): {index_url}")
    lines.append(
        f"📄 {len(done)} module mới · ♻️ {len(skipped)} bỏ qua (đã có) · "
        f"❌ {len(errors)} lỗi"
    )
    if spec_url:
        lines.append(f"📋 SPEC: {spec_url}")
    for r in done:
        lines.append(f"• ✅ `{r['module']}` → {r['url']}")
    for r in skipped:
        lines.append(f"• ♻️ `{r['module']}` → {r['url']}")
    for r in errors:
        lines.append(f"• ❌ `{r['module']}` — {r.get('error', '')}")
    if dropped:
        lines.append(
            f"⚠️ Đã cắt {dropped} module do vượt REVAMP_MODULE_CAP "
            f"({settings.revamp_module_cap}). Thu hẹp scope hoặc tăng cap rồi chạy lại."
        )
    return "\n".join(lines)
