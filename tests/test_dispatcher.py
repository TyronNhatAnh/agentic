import pytest

# AGENTIC_DB / AGENTIC_SERVICES_JSON are set in tests/conftest.py, which pytest
# imports before this module so the agentic.config settings singleton picks them up.
from agentic import dispatcher  # noqa: E402
from agentic.store import connect, init_db, resolve_service_by_github_repo  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    init_db()


# ============================================================================
# Service registry resolution (used by _resolve_active_workspace)
# ============================================================================


def test_seeded_service_resolves_by_github_repo():
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO service_repos(name, repo_path, github_repo, "
            "base_branch_template, jira_board_id, aliases) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "ggx-kr-user-service",
                "/tmp/ggx-kr-user-service",
                "gogovan/ggx-kr-user-service",
                "",
                0,
                '["user-service"]',
            ),
        )
    svc = resolve_service_by_github_repo("gogovan/ggx-kr-user-service")

    assert svc is not None
    assert svc["name"] == "ggx-kr-user-service"


def test_bare_repo_resolution_rejects_ambiguous_matches(tmp_path):
    repo_a = tmp_path / "owner-a-same-service"
    repo_b = tmp_path / "owner-b-same-service"
    repo_a.mkdir()
    repo_b.mkdir()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO service_repos(name, repo_path, github_repo, "
            "base_branch_template, jira_board_id, aliases) VALUES (?, ?, ?, ?, ?, ?)",
            ("same-service-a", str(repo_a), "owner-a/same-service", "", 0, "[]"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO service_repos(name, repo_path, github_repo, "
            "base_branch_template, jira_board_id, aliases) VALUES (?, ?, ?, ?, ?, ?)",
            ("same-service-b", str(repo_b), "owner-b/same-service", "", 0, "[]"),
        )

    assert resolve_service_by_github_repo("owner-b/same-service")["name"] == "same-service-b"
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_service_by_github_repo("same-service")


# ============================================================================
# SDK brain delegation (the only path post-cutover)
# ============================================================================


async def test_dispatcher_delegates_to_brain_session(monkeypatch):
    """handle_message delegates the whole turn to run_brain_session and logs only
    the brain summary row — tool rows come from the hooks (§12.J) — carrying the
    session usage/cost for the observability columns (§12.K)."""
    from agentic.sdk.brain_session import BrainResult
    from agentic import store

    monkeypatch.setattr(dispatcher, "_brain_pool_singleton", lambda: object())
    monkeypatch.setattr(dispatcher, "_pending_singleton", lambda: object())

    captured_brain_args: dict = {}

    async def fake_run_brain_session(**kwargs):
        captured_brain_args.update(kwargs)
        return BrainResult(
            reply="hello from sdk",
            session_id="sess-xyz",
            usage={"input_tokens": 10, "cache_read_input_tokens": 42},
            cost_usd=0.123,
            duration_ms=123,
            num_turns=3,
            tool_use_count=2,
        )

    monkeypatch.setattr(dispatcher, "run_brain_session", fake_run_brain_session)

    logged: list[dict] = []
    real_log_run = store.log_run

    def capture_log_run(**kwargs):
        logged.append(kwargs)
        return real_log_run(**kwargs)

    monkeypatch.setattr(dispatcher, "log_run", capture_log_run)

    out = await dispatcher.handle_message(
        "ping",
        thread_ts="t-sdk",
        channel="C1",
        user_id="U1",
        slack_client=object(),
        placeholder_ts="1700000000.0",
    )

    assert "hello from sdk" in out
    assert captured_brain_args["user_text"] == "ping"
    assert captured_brain_args["thread_ts"] == "t-sdk"

    # Only the brain row — tool rows come from hooks now.
    agents = [e["agent"] for e in logged]
    assert agents == ["brain"]
    assert logged[0]["status"] == "ok"
    # Observability columns forwarded to log_run (§12.K).
    assert logged[0]["usage"] == {"input_tokens": 10, "cache_read_input_tokens": 42}
    assert logged[0]["cost_usd"] == 0.123
    assert logged[0]["num_turns"] == 3


async def test_dispatcher_fails_fast_without_slack_handle():
    with pytest.raises(RuntimeError, match="slack_client"):
        await dispatcher.handle_message(
            "ping", thread_ts="t-x", channel="C1", user_id="U1"
        )
