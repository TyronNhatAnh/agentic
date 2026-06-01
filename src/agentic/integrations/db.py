"""Read-only MariaDB access for the revamp archaeologist.

Why this exists: the legacy da-api `db/schema.rb` is outdated, and a chunk of the
app's behavior is driven by *config rows* that live only in the database — neither
is recoverable from source. So archaeology needs to see the live schema (via
`information_schema`) and read config tables. The trust boundary is deliberate:
point ``REVAMP_DB_*`` at a LOCAL staging clone with a SELECT-only grant — never
prod, never a write-capable user.

Safety is layered, Python being the boundary (not the model):
1. Statement guard — only a single read-only statement (SELECT / SHOW / DESCRIBE /
   EXPLAIN); anything mutating or multi-statement is rejected before it reaches the
   server.
2. ``SET TRANSACTION READ ONLY`` + ``max_statement_time`` on the session.
3. Row cap — a bare SELECT gets a ``LIMIT`` appended so one query can't dump a table.

A read-only DB grant should still be used; these are defense-in-depth, not a
substitute for least-privilege credentials.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal

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


def _render(safe_sql: str, rows: list[dict]) -> str:
    """A compact, unambiguous text rendering for the model: a header + JSON rows.
    ``display()`` returns a success string verbatim, so this is what the agent
    reads (a dict success would be swallowed to its `message` key)."""
    clean = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    body = json.dumps(clean, ensure_ascii=False, indent=2)
    return f"-- {safe_sql}\n-- {len(rows)} row(s)\n{body}"


def _configured() -> bool:
    return bool(settings.revamp_db_host and settings.revamp_db_name)


async def query(sql: str) -> ToolResult:
    """Run one read-only statement against the configured staging clone."""
    if not _configured():
        return ToolResult.failure(
            "CONFIG",
            "Chưa cấu hình REVAMP_DB_* (host/name). Trỏ vào staging clone local "
            "read-only rồi thử lại.",
        )
    safe, err = guard_sql(sql, row_cap=settings.revamp_db_row_cap)
    if err:
        return err

    try:
        import aiomysql  # imported lazily so the bot runs without the driver installed
    except ImportError:
        return ToolResult.failure(
            "CONFIG", "Thiếu driver `aiomysql` — chạy `make install` lại."
        )

    conn = None
    try:
        conn = await aiomysql.connect(
            host=settings.revamp_db_host,
            port=settings.revamp_db_port,
            user=settings.revamp_db_user,
            password=settings.revamp_db_password,
            db=settings.revamp_db_name,
            connect_timeout=settings.revamp_db_timeout_s,
            autocommit=True,
        )
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # Defense-in-depth: read-only session + per-statement time cap. Wrapped
            # because not every MariaDB build/grant exposes these (best-effort).
            for guard in (
                f"SET SESSION max_statement_time={int(settings.revamp_db_timeout_s)}",
                "SET SESSION TRANSACTION READ ONLY",
            ):
                try:
                    await cur.execute(guard)
                except Exception:  # noqa: BLE001 — guard is best-effort
                    log.debug("revamp db session guard skipped: %s", guard)
            await cur.execute(safe)
            rows = await cur.fetchall()
        return ToolResult.success(_render(safe, list(rows or [])))
    except Exception as e:  # noqa: BLE001 — boundary; map to a user-facing code
        msg = str(e).split("\n", 1)[0]
        log.warning("revamp db_query failed: %s", msg)
        return ToolResult.failure("SERVER", f"DB query lỗi: {msg}", retryable=True)
    finally:
        if conn is not None:
            conn.close()
