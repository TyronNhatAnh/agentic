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
"""

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
        # Migrate existing DBs that pre-date the new columns.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(threads)")}
        for col, decl in _THREAD_ADDED_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} {decl}")


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


def recent_runs_for_thread(thread_ts: str, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT agent, input, output, status, created_at FROM runs "
            "WHERE thread_ts = ? ORDER BY id DESC LIMIT ?",
            (thread_ts, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
