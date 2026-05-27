import pytest

# AGENTIC_DB / AGENTIC_SERVICES_JSON are set in tests/conftest.py, which pytest
# imports before this module so the agentic.config settings singleton picks them up.
from agentic.agents import ba, dev, po, review  # noqa: E402
from agentic import dispatcher  # noqa: E402
from agentic.brain import Action, BrainDecision, Step  # noqa: E402
from agentic.integrations.result import ToolResult  # noqa: E402
from agentic.store import connect, init_db, resolve_service_by_github_repo  # noqa: E402


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    init_db()
    monkeypatch.setattr(dispatcher, "maybe_schedule_summary", lambda thread_ts: None)


async def _fake_decide(message, *, summary=None, messages=None, workspace_hint=None):
    return BrainDecision(
        reply=None,
        steps=[Step(agent="ba", task="write user story for: " + message)],
        raw="(mocked)",
    )


async def _fake_ba(task, context=""):
    return f"STORY for task: {task}"


async def test_dispatcher_runs_single_agent(monkeypatch):
    monkeypatch.setattr(dispatcher, "decide", _fake_decide)
    monkeypatch.setitem(dispatcher.REGISTRY, "ba", _fake_ba)

    out = await dispatcher.handle_message(
        "login feature", thread_ts="t1", channel="C1", user_id="U1"
    )
    assert "STORY for task" in out
    assert "[ba]" in out


async def test_subagents_append_prompts_to_claude_code_default(monkeypatch):
    calls = []

    async def fake_run_claude(system_prompt, user_prompt, **kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(ba, "run_claude", fake_run_claude)
    monkeypatch.setattr(po, "run_claude", fake_run_claude)
    monkeypatch.setattr(dev, "run_claude", fake_run_claude)
    monkeypatch.setattr(review, "run_claude", fake_run_claude)

    await ba.run_ba("story")
    await po.run_po("prd")
    await dev.run_dev("fix", cwd="/tmp/repo")
    await review.run_review("review", cwd="/tmp/repo")

    assert [c.get("prompt_mode") for c in calls] == ["append", "append", "append", "append"]
    assert calls[2]["cwd"] == "/tmp/repo"
    assert calls[3]["cwd"] == "/tmp/repo"


async def test_dispatcher_clarification(monkeypatch):
    async def clarify(msg, *, summary=None, messages=None, workspace_hint=None):
        return BrainDecision(need_clarification=True, clarify_question="Which repo?")

    monkeypatch.setattr(dispatcher, "decide", clarify)
    out = await dispatcher.handle_message(
        "do stuff", thread_ts="t2", channel="C1", user_id="U1"
    )
    assert "Which repo?" in out


async def test_dispatcher_sanitizes_casual_pronouns(monkeypatch):
    async def casual_reply(msg, *, summary=None, messages=None, workspace_hint=None):
        return BrainDecision(reply="Muốn tao retry scan cho mày không?", raw="(mocked)")

    monkeypatch.setattr(dispatcher, "decide", casual_reply)
    out = await dispatcher.handle_message(
        "hello", thread_ts="t-tone", channel="C1", user_id="U1"
    )

    assert "Muốn mình retry scan cho bạn không?" in out
    assert "tao" not in out.lower()
    assert "mày" not in out.lower()


async def test_dispatcher_auto_reviews_fetched_pr_diff(monkeypatch):
    diff = "*PR #431 `gogovan/ggx-kr-user-service` diff*:\n```diff\n+new code\n```"
    seen = {}

    async def decide_review_pr(msg, *, summary=None, messages=None, workspace_hint=None):
        return BrainDecision(
            reply=None,
            actions=[
                Action(
                    type="github.get_pr_diff",
                    payload={"repo": "gogovan/ggx-kr-user-service", "pr": 431},
                )
            ],
            raw="(mocked)",
        )

    async def fake_run_action(action):
        return ToolResult.success(diff)

    async def fake_prepare_pr_review_workspace(repo, pr):
        return ToolResult.success(
            {
                "service": "ggx-kr-user-service",
                "repo_path": "/tmp/pr-431",
                "sha": "abc123",
                "message": "Local PR workspace ready: `/tmp/pr-431`.",
            }
        )

    async def fake_review(task, context="", cwd=None):
        seen["task"] = task
        seen["context"] = context
        seen["cwd"] = cwd
        return "🔍 **Review: gogovan/ggx-kr-user-service#431**\n\n### ⛔ Blocking issues\nNone"

    async def fail_shrink(text):
        raise AssertionError("review output should not be passed through reply shrinker")

    monkeypatch.setattr(dispatcher, "decide", decide_review_pr)
    monkeypatch.setattr(dispatcher, "_run_action", fake_run_action)
    monkeypatch.setattr(
        dispatcher.git_int,
        "prepare_pr_review_workspace",
        fake_prepare_pr_review_workspace,
    )
    monkeypatch.setattr(dispatcher, "_shrink_reply", fail_shrink)
    monkeypatch.setitem(dispatcher.REGISTRY, "review", fake_review)

    out = await dispatcher.handle_message(
        "review pr https://github.com/gogovan/ggx-kr-user-service/pull/431",
        thread_ts="t-review-auto-fetched-pr-diff",
        channel="C1",
        user_id="U1",
    )

    assert "[review]" in out
    assert "Blocking issues" in out
    assert diff in seen["context"]
    assert "Local PR workspace ready" in seen["context"]
    assert seen["cwd"] == "/tmp/pr-431"
    assert "PR #431" in seen["task"]


async def test_dispatcher_fixes_fetched_pr_diff_in_local_workspace(monkeypatch):
    diff = "*PR #431 `gogovan/ggx-kr-user-service` diff*:\n```diff\n+buggy code\n```"
    seen = {}

    async def decide_fix_pr(msg, *, summary=None, messages=None, workspace_hint=None):
        return BrainDecision(
            reply=None,
            actions=[
                Action(
                    type="github.get_pr_diff",
                    payload={"repo": "gogovan/ggx-kr-user-service", "pr": 431},
                )
            ],
            raw="(mocked)",
        )

    async def fake_run_action(action):
        return ToolResult.success(diff)

    async def fake_prepare_pr_review_workspace(repo, pr):
        return ToolResult.success(
            {
                "service": "ggx-kr-user-service",
                "repo_path": "/tmp/pr-431",
                "sha": "abc123",
                "message": "Local PR workspace ready: `/tmp/pr-431`.",
            }
        )

    async def fake_dev(task, context="", cwd=None, apply_changes=False):
        seen["task"] = task
        seen["context"] = context
        seen["cwd"] = cwd
        seen["apply_changes"] = apply_changes
        return "Đã sửa handler.go và chạy go test ./internal/..."

    monkeypatch.setattr(dispatcher, "decide", decide_fix_pr)
    monkeypatch.setattr(dispatcher, "_run_action", fake_run_action)
    monkeypatch.setattr(
        dispatcher.git_int,
        "prepare_pr_review_workspace",
        fake_prepare_pr_review_workspace,
    )
    async def fake_shrink(text):
        return text

    monkeypatch.setattr(dispatcher, "_shrink_reply", fake_shrink)
    monkeypatch.setitem(dispatcher.REGISTRY, "dev", fake_dev)

    out = await dispatcher.handle_message(
        "fix 3 critical trong PR https://github.com/gogovan/ggx-kr-user-service/pull/431",
        thread_ts="t-fix-pr",
        channel="C1",
        user_id="U1",
    )

    assert "[dev]" in out
    assert "Đã sửa handler.go" in out
    assert diff in seen["context"]
    assert seen["cwd"] == "/tmp/pr-431"
    assert seen["apply_changes"] is True
    assert "Fix request" in seen["task"]


async def test_dev_first_step_receives_thread_analysis_context(monkeypatch):
    seen = {}
    analysis = (
        "Phân tích fix đã chốt: dùng Redis SET NX EX ở "
        "app/services/order_service.rb:74, tránh race condition lock."
    )

    async def decide_dev(msg, *, summary=None, messages=None, workspace_hint=None):
        return BrainDecision(
            reply=None,
            steps=[Step(agent="dev", task="fix race condition lock")],
            raw="(mocked)",
        )

    async def fake_run_dev(task, context="", cwd=None, apply_changes=False):
        seen["task"] = task
        seen["context"] = context
        seen["cwd"] = cwd
        seen["apply_changes"] = apply_changes
        return "Đã sửa lock"

    monkeypatch.setattr(dispatcher, "decide", decide_dev)
    monkeypatch.setattr(dispatcher, "_run_dev_direct", fake_run_dev)

    out = await dispatcher.handle_message(
        "ok fix",
        thread_ts="t-dev-context",
        channel="C1",
        user_id="U1",
        thread_history=[
            {"role": "assistant", "text": analysis},
            {"role": "user", "text": "ok fix"},
        ],
    )

    assert "[dev]" in out
    assert "app/services/order_service.rb:74" in seen["context"]
    assert "Redis SET NX EX" in seen["context"]
    assert seen["task"] == "fix race condition lock"


async def test_dispatcher_checks_local_repo_status_without_jira_ticket(monkeypatch):
    seen = {}

    async def fail_decide(*args, **kwargs):
        raise AssertionError("repo status question should bypass brain")

    async def fake_run_action(action):
        seen["action"] = action
        return ToolResult.success("Có repo local cho `gogovan/ggx-kr-user-service` nha")

    monkeypatch.setattr(dispatcher, "decide", fail_decide)
    monkeypatch.setattr(dispatcher, "_run_action", fake_run_action)

    thread_ts = "t-repo-status"
    await dispatcher.handle_message(
        "review pr https://github.com/gogovan/ggx-kr-user-service/pull/431",
        thread_ts=thread_ts,
        channel="C1",
        user_id="U1",
    )

    out = await dispatcher.handle_message(
        "ko cần jira tui đang hỏi có repo chưa thôi",
        thread_ts=thread_ts,
        channel="C1",
        user_id="U1",
    )

    assert "Có repo local" in out
    assert seen["action"].type == "git.check_repo"
    assert seen["action"].payload == {"repo": "gogovan/ggx-kr-user-service"}


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


def test_dev_cwd_explicit_current_repo_beats_thread_history(tmp_path):
    repo_a = tmp_path / "service-a"
    repo_b = tmp_path / "service-b"
    repo_a.mkdir()
    repo_b.mkdir()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO service_repos(name, repo_path, github_repo, "
            "base_branch_template, jira_board_id, aliases) VALUES (?, ?, ?, ?, ?, ?)",
            ("svc-a", str(repo_a), "gogovan/service-a", "", 0, '["service-a"]'),
        )
        conn.execute(
            "INSERT OR REPLACE INTO service_repos(name, repo_path, github_repo, "
            "base_branch_template, jira_board_id, aliases) VALUES (?, ?, ?, ?, ?, ?)",
            ("svc-b", str(repo_b), "gogovan/service-b", "", 0, '["service-b"]'),
        )

    slug, path = dispatcher._dev_cwd_from_context(
        {"repo": "gogovan/service-a"},
        text="fix https://github.com/gogovan/service-b/pull/9",
        prior_messages=[{"role": "assistant", "text": "Earlier analysis for service-a"}],
    )

    assert slug == "gogovan/service-b"
    assert path == str(repo_b)


def test_dev_cwd_explicit_unknown_repo_does_not_fallback_to_history(tmp_path):
    repo_a = tmp_path / "service-a"
    repo_a.mkdir()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO service_repos(name, repo_path, github_repo, "
            "base_branch_template, jira_board_id, aliases) VALUES (?, ?, ?, ?, ?, ?)",
            ("svc-a", str(repo_a), "gogovan/service-a", "", 0, '["service-a"]'),
        )

    slug, path = dispatcher._dev_cwd_from_context(
        {"repo": "gogovan/service-a"},
        text="fix https://github.com/gogovan/unknown-service/pull/9",
        prior_messages=[{"role": "assistant", "text": "Earlier analysis for service-a"}],
    )

    assert slug is None
    assert path is None


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


async def test_dispatcher_synthesizes_read_action_outputs(monkeypatch):
    seen = {}

    async def decide_get_issue(msg, *, summary=None, messages=None, workspace_hint=None):
        return BrainDecision(
            reply=None,
            actions=[Action(type="jira.get_issue", payload={"key": "KRP-123"})],
            raw="(mocked)",
        )

    async def fake_run_action(action):
        return ToolResult.success(
            "*KRP-123* — Driver docs\n\n*Description / specs:*\nLookup driver by user ID."
        )

    async def fake_synthesize(**kwargs):
        seen.update(kwargs)
        return "KRP-123 specs: lookup driver by user ID."

    monkeypatch.setattr(dispatcher, "decide", decide_get_issue)
    monkeypatch.setattr(dispatcher, "_run_action", fake_run_action)
    monkeypatch.setattr(dispatcher, "_synthesize_action_reply", fake_synthesize)

    out = await dispatcher.handle_message(
        "docs/specs KRP-123 là gì?",
        thread_ts="t-jira-docs",
        channel="C1",
        user_id="U1",
    )

    assert "KRP-123 specs: lookup driver by user ID." in out
    assert "🛠️" in out  # footer line appended
    assert seen["user_text"] == "docs/specs KRP-123 là gì?"
    assert seen["tool_outputs"][0][0] == "jira.get_issue"
    assert "Lookup driver" in seen["tool_outputs"][0][1]


def test_has_log_output_only_for_loki():
    assert dispatcher._has_log_output([("grafana.search_logs", "...")]) is True
    assert dispatcher._has_log_output([("jira.get_issue", "...")]) is False
    assert dispatcher._has_log_output([]) is False
    # mixed: log present anywhere → True
    assert dispatcher._has_log_output(
        [("jira.get_issue", "..."), ("grafana.search_logs", "...")]
    ) is True


async def test_synthesize_injects_grounding_rules_for_logs(monkeypatch):
    captured = {}

    async def fake_run_claude(system, user, **kwargs):
        captured["system"] = system
        return "ok"

    monkeypatch.setattr(dispatcher, "run_claude", fake_run_claude)

    # logs present → grounding block must be appended
    await dispatcher._synthesize_action_reply(
        user_text="tóm tắt lỗi order 2717068",
        tool_outputs=[("grafana.search_logs", "PUT /orders/2717068/assign 200")],
        summary=None,
    )
    assert "XUẤT HIỆN NGUYÊN VĂN" in captured["system"]
    assert "không có trong log" in captured["system"]
    assert "truncated" in captured["system"]

    # no logs → no grounding block
    captured.clear()
    await dispatcher._synthesize_action_reply(
        user_text="docs KRP-123",
        tool_outputs=[("jira.get_issue", "*KRP-123* specs")],
        summary=None,
    )
    assert "XUẤT HIỆN NGUYÊN VĂN" not in captured["system"]
