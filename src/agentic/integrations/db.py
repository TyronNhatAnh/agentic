"""Read-only DB introspection (db_query / db_query_prod tools) via the order-service debug API.

Why this exists: the brain sometimes needs the *live* schema + config rows to
debug — a chunk of behavior is driven by config rows that live only in the
database, and the checked-in schema can lag reality.

The DB sits behind a VPN the bot host can't reach, so the old direct MariaDB
connection was unusable. We now go through ``ggx-kr-order-service``'s
``POST /api/v1/admin/orders/debug/query`` admin endpoint instead — it runs one
read-only statement against the read replica and returns ``{"rowCount", "rows"}``.
The route ships on all envs (PR #1084 removed the env gate); ``query`` targets
staging and ``query_prod`` targets the prod replica (``@@read_only=1``). The prod
path hits real customer PII, so its tool (``db_query_prod``) is in CONFIRM_TOOLS
and always prompts for a Slack button before it runs.

Safety is layered — the server validates (allowed prefixes, banned DML keywords,
single statement, LIMIT 1000, 15s timeout, audit log), and Python adds a client-side
guard so an obviously-bad statement fails fast without a network round-trip / audit
entry:
1. Statement guard — only a single read-only statement (SELECT / WITH / SHOW /
   DESCRIBE / EXPLAIN); anything mutating or multi-statement is rejected locally.
2. Row cap — a bare SELECT gets a ``LIMIT`` appended (stricter than the server's
   1000) so one query can't bloat the transcript.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal

import httpx

from ..config import settings
from .result import ToolResult

log = logging.getLogger(__name__)

# Read-only statement openers. MariaDB treats all of these as non-mutating; every
# other verb (INSERT/UPDATE/DELETE/REPLACE/CALL/SET/CREATE/DROP/…) is refused.
_READ_PREFIXES = ("select", "show", "describe", "desc", "explain", "with")

# Strip leading SQL comments so a statement can't smuggle a verb past the prefix
# check behind `/* */` or `-- ` noise.
_LEADING_COMMENT_RE = re.compile(r"^(?:\s*(?:/\*.*?\*/|--[^\n]*\n|#[^\n]*\n))+", re.DOTALL)
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)


def _strip(sql: str) -> str:
    prev = None
    out = sql.strip()
    while prev != out:
        prev = out
        out = _LEADING_COMMENT_RE.sub("", out).strip()
    return out


def guard_sql(sql: str, *, row_cap: int) -> tuple[str | None, ToolResult | None]:
    """Validate a single read-only statement and return (safe_sql, None) or
    (None, failure). Pure function — unit-tested without a DB connection."""
    raw = (sql or "").strip()
    if not raw:
        return None, ToolResult.failure("VALIDATION", "SQL rỗng.")
    body = _strip(raw).rstrip(";").strip()
    if not body:
        return None, ToolResult.failure("VALIDATION", "SQL rỗng sau khi bỏ comment.")
    # Reject multiple statements: any `;` left mid-body means a second statement.
    if ";" in body:
        return None, ToolResult.failure(
            "VALIDATION", "Chỉ cho phép 1 câu lệnh; bỏ phần sau dấu `;`."
        )
    low = body.lower()
    if not low.startswith(_READ_PREFIXES):
        return None, ToolResult.failure(
            "VALIDATION",
            "Chỉ cho phép câu đọc (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH). "
            "Tool này read-only.",
        )
    # Append a LIMIT to bare SELECT/WITH so a query can't dump an entire table.
    # SHOW/DESCRIBE/EXPLAIN are inherently bounded, so they're left alone.
    if low.startswith(("select", "with")) and not _LIMIT_RE.search(low):
        body = f"{body} LIMIT {int(row_cap)}"
    return body, None


def _jsonable(v):
    """Coerce DB value types the model shouldn't choke on into JSON scalars."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def _render(safe_sql: str, rows: list[dict], row_count: int | None = None) -> str:
    """A compact, unambiguous text rendering for the model: a header + JSON rows.
    ``display()`` returns a success string verbatim, so this is what the agent
    reads (a dict success would be swallowed to its `message` key)."""
    clean = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    body = json.dumps(clean, ensure_ascii=False, indent=2)
    n = len(rows) if row_count is None else row_count
    return f"-- {safe_sql}\n-- {n} row(s)\n{body}"


_ENDPOINT = "/api/v1/admin/orders/debug/query"

# Cloudflare fronts the prod host and 1010-blocks a default httpx UA; send a
# browser UA (matches the prod-db-query skill's query.sh). Harmless on staging.
_BROWSER_UA = "Mozilla/5.0"


def _map_http_error(status: int, body: str, *, base_url_env: str) -> ToolResult:
    """Translate the debug-API HTTP status into a user-facing ToolResult.

    The server returns short bodies (e.g. ``accessToken``, ``PerMissionDenied``,
    ``only a single read-only query is allowed``, or a raw MySQL error); we relay a
    truncated form. AUTH/VALIDATION/CONFIG are terminal (no retry — a 400/401 won't
    pass on a second identical call); 5xx is transient. ``base_url_env`` names the
    env var to check so the 404 hint points at the right (staging vs prod) config.
    """
    snippet = (body or "").strip()[:300]
    if status in (401, 403):
        return ToolResult.failure(
            "AUTH",
            f"Debug query API {status}: {snippet or 'token hết hạn / không có quyền AdminUser'}.",
        )
    if status == 404:
        return ToolResult.failure(
            "CONFIG",
            f"Debug query API 404: route không tồn tại — kiểm tra {base_url_env} "
            "trỏ đúng host + service đã deploy debug endpoint.",
        )
    if status == 400:
        return ToolResult.failure(
            "VALIDATION", f"Debug query API từ chối: {snippet or 'query không hợp lệ'}."
        )
    return ToolResult.failure(
        "SERVER", f"Debug query API {status}: {snippet}", retryable=status >= 500
    )


async def _run_query(sql: str, *, base_url: str, token: str, base_url_env: str) -> ToolResult:
    """Shared read-only debug-query call. staging (``query``) and prod
    (``query_prod``) differ only in the (base_url, token) target."""
    if not (base_url and token):
        return ToolResult.failure(
            "CONFIG",
            f"Chưa cấu hình {base_url_env} / token tương ứng. "
            "Set host + admin token (role AdminUser) rồi thử lại.",
        )
    safe, err = guard_sql(sql, row_cap=settings.order_debug_row_cap)
    if err:
        return err

    url = base_url.rstrip("/") + _ENDPOINT
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": _BROWSER_UA,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.order_debug_timeout_s) as client:
            r = await client.post(url, headers=headers, json={"query": safe})
    except httpx.TimeoutException:
        return ToolResult.failure(
            "TIMEOUT",
            f"Debug query API timeout (>{settings.order_debug_timeout_s}s). "
            "Câu có thể quá nặng — thêm filter/LIMIT.",
            retryable=True,
        )
    except httpx.HTTPError as e:  # connect/transport errors
        msg = str(e).split("\n", 1)[0]
        log.warning("db_query http error: %s", msg)
        return ToolResult.failure("NETWORK", f"Gọi debug query API lỗi mạng: {msg}", retryable=True)

    if r.status_code != 200:
        return _map_http_error(r.status_code, r.text, base_url_env=base_url_env)

    try:
        payload = r.json()
    except ValueError:
        return ToolResult.failure(
            "SERVER", f"Response không phải JSON: {r.text[:300]}", retryable=True
        )
    rows = list(payload.get("rows") or [])
    row_count = payload.get("rowCount", len(rows))
    return ToolResult.success(_render(safe, rows, row_count))


async def query(sql: str) -> ToolResult:
    """Run one read-only statement against the STAGING read replica via the
    order-service debug-query admin API."""
    return await _run_query(
        sql,
        base_url=settings.order_debug_base_url,
        token=settings.order_debug_admin_token,
        base_url_env="ORDER_DEBUG_BASE_URL",
    )


async def query_prod(sql: str) -> ToolResult:
    """Run one read-only statement against the PRODUCTION read replica via the
    order-service debug-query admin API. Gated by CONFIRM_TOOLS (Slack button) —
    hits real customer PII and is audit-logged."""
    return await _run_query(
        sql,
        base_url=settings.order_debug_prod_base_url,
        token=settings.order_debug_prod_admin_token,
        base_url_env="ORDER_DEBUG_PROD_BASE_URL",
    )
