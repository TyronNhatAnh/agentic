import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import settings

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
    repo TEXT
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
    repo_path TEXT NOT NULL,
    github_repo TEXT,
    base_branch_template TEXT,
    jira_board_id INTEGER,
    aliases TEXT
);

CREATE TABLE IF NOT EXISTS pending_confirmations (
    thread_ts TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    question TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

_SERVICE_SEEDS = [
    {
        "name": "ggx-kr-user-service",
        "repo_path": "/Users/tyron/Documents/work/Gogox/ggx-kr-user-service",
        "github_repo": "gogovan/ggx-kr-user-service",
        "base_branch_template": "",
        "jira_board_id": 0,
        "aliases": '["user", "user-service", "user services", "user service"]',
    },
]

_THREAD_ADDED_COLUMNS = {
    "summary": "TEXT",
    "last_agent": "TEXT",
    "jira_keys": "TEXT",
    "pr_refs": "TEXT",
    "repo": "TEXT",
}

_THREAD_FIELDS = {"summary", "last_agent", "jira_keys", "pr_refs", "repo"}


def init_db() -> None:
    Path(settings.agentic_db).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(threads)")}
        for col, decl in _THREAD_ADDED_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} {decl}")
        # Seed service_repos if empty
        row = conn.execute("SELECT COUNT(*) AS n FROM service_repos").fetchone()
        if row["n"] == 0:
            for s in _SERVICE_SEEDS:
                conn.execute(
                    """
                    INSERT INTO service_repos(name, repo_path, github_repo,
                                              base_branch_template, jira_board_id, aliases)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (s["name"], s["repo_path"], s["github_repo"],
                     s["base_branch_template"], s["jira_board_id"], s["aliases"]),
                )
        else:
            for s in _SERVICE_SEEDS:
                if not s["github_repo"]:
                    continue
                conn.execute(
                    """
                    UPDATE service_repos
                    SET github_repo=?
                    WHERE name=? AND (github_repo IS NULL OR github_repo='')
                    """,
                    (s["github_repo"], s["name"]),
                )


@contextmanager
def connect():
    conn = sqlite3.connect(settings.agentic_db)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs(created_at, thread_ts, channel, user_id, agent,
                             input, output, status, duration_ms, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    """Match a GitHub repo slug to a configured local service."""
    normalized = repo.strip().lower()
    repo_name = normalized.rsplit("/", 1)[-1]
    with connect() as conn:
        rows = conn.execute("SELECT * FROM service_repos").fetchall()
    for r in rows:
        d = dict(r)
        github_repo = (d.get("github_repo") or "").strip().lower()
        if github_repo and github_repo == normalized:
            return d
        if d["name"].lower() == repo_name:
            return d
    return None


def list_services() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM service_repos ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def save_pending_confirmation(thread_ts: str, action_type: str,
                              payload: dict, question: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_confirmations(thread_ts, action_type, payload,
                                              question, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_ts) DO UPDATE SET
                action_type=excluded.action_type,
                payload=excluded.payload,
                question=excluded.question,
                created_at=excluded.created_at
            """,
            (thread_ts, action_type, json.dumps(payload), question, time.time()),
        )


def get_pending_confirmation(thread_ts: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_confirmations WHERE thread_ts=?", (thread_ts,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d["payload"])
    except json.JSONDecodeError:
        d["payload"] = {}
    return d


def clear_pending_confirmation(thread_ts: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pending_confirmations WHERE thread_ts=?", (thread_ts,))


def recent_runs_for_thread(thread_ts: str, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT agent, input, output, status, created_at FROM runs "
            "WHERE thread_ts = ? ORDER BY id DESC LIMIT ?",
            (thread_ts, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
