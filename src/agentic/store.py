import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import settings

_PRAGMAS_APPLIED = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    thread_ts TEXT,
    channel TEXT,
    user_id TEXT,
    agent TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_ts);

CREATE TABLE IF NOT EXISTS threads (
    thread_ts TEXT PRIMARY KEY,
    channel TEXT,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL,
    meta TEXT,
    summary TEXT,
    last_agent TEXT,
    jira_keys TEXT,
    pr_refs TEXT,
    repo TEXT,
    active_ticket TEXT,
    active_worktree TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_ts TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_ts, id);

CREATE TABLE IF NOT EXISTS service_repos (
    name TEXT PRIMARY KEY,
    repo_path TEXT,
    github_repo TEXT,
    base_branch_template TEXT,
    jira_board_id INTEGER,
    aliases TEXT,
    loki_selector TEXT
);

CREATE TABLE IF NOT EXISTS revamp_modules (
    run_key TEXT NOT NULL,
    module TEXT NOT NULL,
    status TEXT NOT NULL,
    doc_url TEXT,
    doc_page_id TEXT,
    error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_key, module)
);

CREATE TABLE IF NOT EXISTS revamp_runs (
    run_key TEXT PRIMARY KEY,
    index_page_id TEXT,
    index_url TEXT,
    updated_at REAL NOT NULL
);
"""

def _load_service_seeds() -> list[dict]:
    """Service seeds come from an external JSON file pointed to by
    AGENTIC_SERVICES_JSON. Empty by default — keeps the repo portable."""
    path = (settings.services_seed_path or "").strip()
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    seeds: list[dict] = []
    for item in raw:
        # repo_path is optional: a service may be registered for log/PR checks only,
        # without a local clone (git/dev worktree ops just won't be available for it).
        if not isinstance(item, dict) or not item.get("name"):
            continue
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            aliases_json = json.dumps(aliases)
        elif isinstance(aliases, str):
            aliases_json = aliases
        else:
            aliases_json = "[]"
        seeds.append({
            "name": item["name"],
            "repo_path": item.get("repo_path") or "",
            "github_repo": item.get("github_repo") or "",
            "base_branch_template": item.get("base_branch_template") or "",
            "jira_board_id": int(item.get("jira_board_id") or 0),
            "aliases": aliases_json,
            "loki_selector": item.get("loki_selector") or "",
        })
    return seeds

_THREAD_ADDED_COLUMNS = {
    "summary": "TEXT",
    "last_agent": "TEXT",
    "jira_keys": "TEXT",
    "pr_refs": "TEXT",
    "repo": "TEXT",
    "active_ticket": "TEXT",
    "active_worktree": "TEXT",
    "sdk_session_id": "TEXT",
    "sdk_state_blob": "TEXT",
}

_THREAD_FIELDS = {
    "summary", "last_agent", "jira_keys", "pr_refs", "repo",
    "active_ticket", "active_worktree",
    "sdk_session_id", "sdk_state_blob",
}

# Columns added to service_repos after its initial release; migrated on startup
# via ALTER TABLE since CREATE TABLE IF NOT EXISTS won't alter an existing table.
_SERVICE_ADDED_COLUMNS = {
    "loki_selector": "TEXT",
}

# Per-turn observability columns (Phase 4 §12.K). Only the brain summary row
# fills them — from ResultMessage.usage; tool rows stay null. Migrated on
# startup like the thread columns above.
_RUNS_ADDED_COLUMNS = {
    "cache_read_input_tokens": "INTEGER",
    "cache_creation_input_tokens": "INTEGER",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "cost_usd": "REAL",
    "num_turns": "INTEGER",
}


def init_db() -> None:
    Path(settings.agentic_db).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(threads)")}
        for col, decl in _THREAD_ADDED_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} {decl}")
        existing_runs = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        for col, decl in _RUNS_ADDED_COLUMNS.items():
            if col not in existing_runs:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
        existing_svc = {row["name"] for row in conn.execute("PRAGMA table_info(service_repos)")}
        for col, decl in _SERVICE_ADDED_COLUMNS.items():
            if col not in existing_svc:
                conn.execute(f"ALTER TABLE service_repos ADD COLUMN {col} {decl}")
        seeds = _load_service_seeds()
        if not seeds:
            return
        # Upsert every seed on each startup so the registry stays in sync with
        # AGENTIC_SERVICES_JSON — adding a new service no longer needs a db-reset.
        # github_repo/aliases/loki_selector come from the seed (source of truth);
        # repo_path/base_branch_template are only overwritten when the seed sets
        # them, so operator-supplied local paths survive.
        for s in seeds:
            conn.execute(
                """
                INSERT INTO service_repos(name, repo_path, github_repo,
                                          base_branch_template, jira_board_id,
                                          aliases, loki_selector)
                VALUES (:name, :repo_path, :github_repo, :base_branch_template,
                        :jira_board_id, :aliases, :loki_selector)
                ON CONFLICT(name) DO UPDATE SET
                    github_repo   = excluded.github_repo,
                    aliases       = excluded.aliases,
                    loki_selector = excluded.loki_selector,
                    jira_board_id = excluded.jira_board_id,
                    repo_path = CASE WHEN excluded.repo_path != ''
                                     THEN excluded.repo_path ELSE service_repos.repo_path END,
                    base_branch_template = CASE WHEN excluded.base_branch_template != ''
                                     THEN excluded.base_branch_template
                                     ELSE service_repos.base_branch_template END
                """,
                s,
            )


@contextmanager
def connect():
    global _PRAGMAS_APPLIED
    conn = sqlite3.connect(settings.agentic_db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    if not _PRAGMAS_APPLIED:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            _PRAGMAS_APPLIED = True
        except sqlite3.DatabaseError:
            pass
    else:
        conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


async def run_db(func, *args, **kwargs):
    """Run a blocking store helper off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def log_run(
    *,
    agent: str,
    input_text: str,
    output: str | None,
    status: str,
    duration_ms: int,
    thread_ts: str | None = None,
    channel: str | None = None,
    user_id: str | None = None,
    error: str | None = None,
    usage: dict | None = None,
    cost_usd: float | None = None,
    num_turns: int | None = None,
) -> int:
    # Observability columns (§12.K) — only the brain summary row passes usage;
    # tool rows leave them null. Token counts are derived from usage so callers
    # forward ResultMessage.usage verbatim.
    u = usage or {}
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs(created_at, thread_ts, channel, user_id, agent,
                             input, output, status, duration_ms, error,
                             cache_read_input_tokens, cache_creation_input_tokens,
                             input_tokens, output_tokens, cost_usd, num_turns)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                thread_ts,
                channel,
                user_id,
                agent,
                input_text,
                output,
                status,
                duration_ms,
                error,
                u.get("cache_read_input_tokens"),
                u.get("cache_creation_input_tokens"),
                u.get("input_tokens"),
                u.get("output_tokens"),
                cost_usd,
                num_turns,
            ),
        )
        return cur.lastrowid


def touch_thread(thread_ts: str, channel: str | None) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO threads(thread_ts, channel, created_at, last_active_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(thread_ts) DO UPDATE SET last_active_at=excluded.last_active_at
            """,
            (thread_ts, channel, now, now),
        )


def update_thread_fields(thread_ts: str, **fields) -> None:
    """Patch one or more of: summary, last_agent, jira_keys, pr_refs, repo."""
    unknown = set(fields) - _THREAD_FIELDS
    if unknown:
        raise ValueError(f"unknown thread fields: {unknown}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [thread_ts]
    with connect() as conn:
        conn.execute(f"UPDATE threads SET {sets} WHERE thread_ts=?", params)


def get_thread(thread_ts: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM threads WHERE thread_ts=?", (thread_ts,)
        ).fetchone()
        return dict(row) if row else None


def add_message(thread_ts: str, role: str, text: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages(thread_ts, role, text, created_at) VALUES (?, ?, ?, ?)",
            (thread_ts, role, text, time.time()),
        )
        return cur.lastrowid


def recent_messages(thread_ts: str, limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, text, created_at FROM messages "
            "WHERE thread_ts=? ORDER BY id DESC LIMIT ?",
            (thread_ts, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def count_messages(thread_ts: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE thread_ts=?", (thread_ts,)
        ).fetchone()
        return int(row["n"])


def resolve_service(name_or_alias: str) -> dict | None:
    """Match a service by canonical name or any alias (case-insensitive)."""
    needle = name_or_alias.strip().lower()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM service_repos").fetchall()
    for r in rows:
        d = dict(r)
        if d["name"].lower() == needle:
            return d
        try:
            aliases = json.loads(d.get("aliases") or "[]")
        except json.JSONDecodeError:
            aliases = []
        if any(a.lower() == needle for a in aliases):
            return d
    return None


def resolve_service_by_github_repo(repo: str) -> dict | None:
    """Match a GitHub repo slug to a configured local service.

    Accepts a full slug (`owner/repo`), a bare repo name (`ggx-kr-da-api`), the
    canonical service name, or any configured alias — so a user typing the repo
    name without the owner prefix still resolves to the local clone. Full slugs
    must match exactly; bare names must resolve to exactly one service.
    """
    normalized = repo.strip().lower()
    if not normalized:
        return None
    has_owner = "/" in normalized
    repo_name = normalized.rsplit("/", 1)[-1]
    with connect() as conn:
        rows = conn.execute("SELECT * FROM service_repos").fetchall()
    matches: list[dict] = []
    for r in rows:
        d = dict(r)
        github_repo = (d.get("github_repo") or "").strip().lower()
        if has_owner:
            if github_repo and github_repo == normalized:
                return d
            continue
        if github_repo and github_repo.rsplit("/", 1)[-1] == repo_name:
            matches.append(d)
            continue
        if d["name"].lower() in (normalized, repo_name):
            matches.append(d)
            continue
        try:
            aliases = json.loads(d.get("aliases") or "[]")
        except json.JSONDecodeError:
            aliases = []
        if any(str(a).lower() in (normalized, repo_name) for a in aliases):
            matches.append(d)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(
            (m.get("github_repo") or m["name"]) for m in matches[:5]
        )
        more = f", ... (+{len(matches) - 5})" if len(matches) > 5 else ""
        raise ValueError(
            f"repo/service `{repo}` ambiguous: {names}{more}. Use full owner/repo."
        )
    # Backward-compatible fallback: service name may be the full input even when
    # it contains a slash-like namespace string but github_repo is not populated.
    for r in rows:
        d = dict(r)
        if d["name"].lower() == normalized:
            return d
    return None


def list_services() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM service_repos ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def upsert_revamp_module(
    run_key: str,
    module: str,
    *,
    status: str,
    doc_url: str | None = None,
    doc_page_id: str | None = None,
    error: str | None = None,
) -> None:
    """Record (or update) the outcome of analysing one module in a revamp run.
    The (run_key, module) primary key makes the pipeline idempotent — a rerun
    sees the prior ``done`` row and skips re-analysing/re-publishing."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO revamp_modules(run_key, module, status, doc_url,
                                       doc_page_id, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_key, module) DO UPDATE SET
                status      = excluded.status,
                doc_url     = excluded.doc_url,
                doc_page_id = excluded.doc_page_id,
                error       = excluded.error,
                updated_at  = excluded.updated_at
            """,
            (run_key, module, status, doc_url, doc_page_id, error, time.time()),
        )


def get_revamp_module(run_key: str, module: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM revamp_modules WHERE run_key=? AND module=?",
            (run_key, module),
        ).fetchone()
        return dict(row) if row else None


def list_revamp_modules(run_key: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM revamp_modules WHERE run_key=? ORDER BY module",
            (run_key,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_revamp_run(run_key: str) -> dict | None:
    """The per-run Notion index page, so a rerun nests new module pages under the
    same index instead of creating a fresh one."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM revamp_runs WHERE run_key=?", (run_key,)
        ).fetchone()
        return dict(row) if row else None


def upsert_revamp_run(run_key: str, index_page_id: str, index_url: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO revamp_runs(run_key, index_page_id, index_url, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_key) DO UPDATE SET
                index_page_id = excluded.index_page_id,
                index_url     = excluded.index_url,
                updated_at    = excluded.updated_at
            """,
            (run_key, index_page_id, index_url, time.time()),
        )


def recent_runs_for_thread(thread_ts: str, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT agent, input, output, status, created_at FROM runs "
            "WHERE thread_ts = ? ORDER BY id DESC LIMIT ?",
            (thread_ts, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
