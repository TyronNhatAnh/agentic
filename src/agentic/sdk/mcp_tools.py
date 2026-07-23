"""In-process MCP server exposing integration verbs as typed SDK tools.

Phase 2: every legacy ``integrations/*.execute_action`` verb is exposed 1-to-1
as an ``@tool`` with a JSON Schema input. The brain stops parsing JSON action
blobs and starts calling these tools via SDK function calling. Retry is owned
here (read-only verbs only) so Claude never burns tokens on transient jitter
and the conversation prefix stays cache-stable.

Naming: ``<integration>_<verb>`` snake_case — MCP tool names cannot contain
``.``, and snake_case matches ``CONFIRM_TOOLS`` in ``permission.py``. The
mapping to legacy ``action_type`` is a literal ``.`` ↔ ``_`` swap.

Confirm contract (§12.I): ``github_approve_pr`` and ``github_merge_pr`` are
invoked with ``confirmed=True`` here — Phase 1 ``can_use_tool`` callback owns
the user prompt. ``git_prepare_workspace`` and ``git_commit`` also pass
``confirmed=True`` because the SDK path does not bubble ``NEEDS_CONFIRMATION``
results to Slack; if those tools need a guard later, add them to
``CONFIRM_TOOLS`` rather than re-enabling the legacy prompt.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..integrations import db as db_int
from ..integrations import git as git_int
from ..integrations import github as github_int
from ..integrations import grafana as grafana_int
from ..integrations import jira as jira_int
from ..integrations import notion as notion_int
from ..integrations import ship as ship_int
from ..store import list_services as _list_services
from ..integrations.result import ToolResult, classify_exception

log = logging.getLogger(__name__)

# Curated map of the shared legacy `gogovan` schema (naming convention + core
# tables). Referenced from the db_query* descriptions so the brain Reads it on
# demand instead of guessing table names from model/struct conventions.
_DB_TABLES_DOC = Path(__file__).resolve().parents[3] / "docs" / "DB_TABLES.md"
_DB_SCHEMA_HINT = (
    f"Before your first query in a session, Read {_DB_TABLES_DOC} — legacy "
    "naming (table `orderrequest`, not `order_requests`), core tables, `*CD` "
    "code lookup. "
)

# Curated map of the GoGoX KR backend (services, call graph, domain flows).
# Referenced from github_get_pr_diff so a reviewer Reads it before judging
# cross-service correctness instead of reviewing a diff in a vacuum.
_ARCH_DOC = Path(__file__).resolve().parents[3] / "docs" / "GOGOX_ARCHITECTURE.md"
_ARCH_HINT = (
    f" Before reviewing a GoGoX service PR, Read {_ARCH_DOC} — a small INDEX with "
    "the topology, call graph and naming traps ('DaService', report-service). Then "
    "Read docs/arch/features.md (next to the index) to find which feature the change "
    "touches and its FULL set of services, and Read docs/arch/<service>.md for each of "
    "those — the repo the PR sits in is rarely the whole story. Don't load every service."
)

# Match dispatcher.py:103-104 legacy semantics — read-only verbs retry up to
# 2 times on transient errors, write verbs never retry (timed-out POST may
# have succeeded server-side, retry would duplicate).
_MAX_RETRIES = 2
_RETRY_BACKOFF_S = (0.5, 1.5)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text or ""}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text or "error"}], "is_error": True}


async def _run_with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    retryable_read: bool,
    service: str,
    retry_timeout: bool = True,
) -> dict[str, Any]:
    """Call a legacy integration fn. Translate ``ToolResult`` → MCP content.

    Read-only verbs retry transient ``retryable=True`` failures
    (TIMEOUT/NETWORK/SERVER/RATE_LIMIT) up to ``_MAX_RETRIES`` with backoff.
    Write verbs (``retryable_read=False``) never retry. Raw exceptions are
    classified via ``classify_exception`` so the brain sees AUTH/CONFIG/etc.
    instead of a stack trace.

    ``retry_timeout=False`` excludes TIMEOUT from the retryable set (still
    retries NETWORK/SERVER/RATE_LIMIT). Use for backends where a timeout means
    the request itself is too heavy — retrying the same query just multiplies
    wall time and re-times-out (e.g. Loki log searches). NETWORK/SERVER are
    still genuinely transient and worth a retry.
    """
    last: ToolResult | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            result = await fn()
        except Exception as e:  # noqa: BLE001 — boundary, want all
            result = classify_exception(e, service=service)

        if isinstance(result, str):
            return _ok(result)
        if not isinstance(result, ToolResult):
            return _ok(str(result))

        last = result
        if result.ok:
            return _ok(result.display())

        if (
            not retryable_read
            or not result.retryable
            or (not retry_timeout and result.error_code == "TIMEOUT")
            or attempt >= _MAX_RETRIES
        ):
            break
        delay = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
        log.info(
            "mcp tool %s retry %d/%d after %.1fs (%s)",
            service, attempt + 1, _MAX_RETRIES, delay, result.error_code,
        )
        await asyncio.sleep(delay)

    assert last is not None
    msg = f"[{last.error_code}] {last.user_message or ''}".strip()
    return _err(msg)


# ============================================================================
# GitHub (13)
# ============================================================================

@tool(
    "github_create_issue",
    "Create a new GitHub issue. Use when the user asks to file/open a bug or task.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "repo": {"type": "string", "description": "owner/repo; defaults to GITHUB_DEFAULT_REPO"},
        },
        "required": ["title", "body"],
    },
)
async def github_create_issue(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.create_issue(args["title"], args["body"], args.get("repo")),
        retryable_read=False, service="GitHub",
    )


@tool(
    "github_create_pr",
    "Open a new pull request. Use only when there's no local worktree — otherwise prefer ship_create_pr.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "head": {"type": "string", "description": "feature branch name"},
            "base": {"type": "string", "description": "target branch, e.g. releases/DAPro-2.X"},
            "body": {"type": "string"},
            "repo": {"type": "string"},
            "draft": {"type": "boolean"},
        },
        "required": ["title", "head", "base"],
    },
)
async def github_create_pr(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.create_pr(
            args["title"], args["head"], args["base"],
            args.get("body", ""), args.get("repo"),
            bool(args.get("draft", False)),
        ),
        retryable_read=False, service="GitHub",
    )


@tool(
    "github_comment_pr",
    "Post a comment on a pull request.",
    {
        "type": "object",
        "properties": {
            "pr": {"type": "integer"},
            "body": {"type": "string"},
            "repo": {"type": "string"},
        },
        "required": ["pr", "body"],
    },
)
async def github_comment_pr(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.comment_pr(args["pr"], args["body"], args.get("repo")),
        retryable_read=False, service="GitHub",
    )


@tool(
    "github_add_assignees",
    "Assign one or more GitHub users to a PR (or issue). Users must be repo "
    "collaborators; ones without access are reported as skipped. Use this instead "
    "of shell `gh` — it calls the REST API directly (no quoting pitfalls).",
    {
        "type": "object",
        "properties": {
            "pr": {"type": "integer"},
            "assignees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "GitHub logins, with or without leading @.",
            },
            "repo": {"type": "string"},
        },
        "required": ["pr", "assignees"],
    },
)
async def github_add_assignees(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.add_assignees(
            args["pr"], args["assignees"], args.get("repo")
        ),
        retryable_read=False, service="GitHub",
    )


@tool(
    "github_approve_pr",
    "Submit an APPROVE review on a pull request. User confirmation is handled by the permission callback — do not ask twice.",
    {
        "type": "object",
        "properties": {
            "pr": {"type": "integer"},
            "repo": {"type": "string"},
            "body": {"type": "string", "description": "optional approval comment"},
        },
        "required": ["pr"],
    },
)
async def github_approve_pr(args: dict[str, Any]) -> dict[str, Any]:
    # confirmed=True: SDK can_use_tool callback (§12.A) gated this call already.
    return await _run_with_retry(
        lambda: github_int.approve_pr(
            args["pr"], args.get("repo"), args.get("body", ""), confirmed=True,
        ),
        retryable_read=False, service="GitHub",
    )


@tool(
    "github_merge_pr",
    "Merge a pull request. Re-checks mergeable_state — refuses dirty/blocked/behind/draft. User confirmation is handled by the permission callback.",
    {
        "type": "object",
        "properties": {
            "pr": {"type": "integer"},
            "repo": {"type": "string"},
            "method": {"type": "string", "enum": ["squash", "merge", "rebase"]},
            "commit_title": {"type": "string"},
            "commit_message": {"type": "string"},
        },
        "required": ["pr"],
    },
)
async def github_merge_pr(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.merge_pr(
            args["pr"], args.get("repo"),
            args.get("method", "squash"),
            args.get("commit_title", ""),
            args.get("commit_message", ""),
            confirmed=True,
        ),
        retryable_read=False, service="GitHub",
    )


@tool(
    "github_update_pr",
    "Update an existing PR's base/title/body/draft. At least one of base/title/body/draft must be supplied.",
    {
        "type": "object",
        "properties": {
            "pr": {"type": "integer"},
            "repo": {"type": "string"},
            "base": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "draft": {"type": "boolean"},
        },
        "required": ["pr"],
    },
)
async def github_update_pr(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.update_pr(
            args["pr"], args.get("repo"),
            args.get("base"), args.get("title"),
            args.get("body"),
            args["draft"] if "draft" in args else None,
        ),
        retryable_read=False, service="GitHub",
    )


@tool(
    "github_list_my_prs",
    "List PRs where the bot user is author/assignee/reviewer.",
    {
        "type": "object",
        "properties": {
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
        },
        "required": [],
    },
)
async def github_list_my_prs(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.list_my_prs(args.get("state", "open")),
        retryable_read=True, service="GitHub",
    )


@tool(
    "github_list_prs",
    "List PRs in a repo, optionally filtered by author.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "author": {"type": "string"},
        },
        "required": ["repo"],
    },
)
async def github_list_prs(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.list_prs(
            args["repo"], args.get("state", "open"), args.get("author"),
        ),
        retryable_read=True, service="GitHub",
    )


@tool(
    "github_list_issues",
    "List issues in a repo, optionally filtered by assignee/label.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "assignee": {"type": "string"},
            "label": {"type": "string"},
        },
        "required": ["repo"],
    },
)
async def github_list_issues(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.list_issues(
            args["repo"], args.get("state", "open"),
            args.get("assignee"), args.get("label"),
        ),
        retryable_read=True, service="GitHub",
    )


@tool(
    "github_list_notifications",
    "List the bot's GitHub notification inbox. Set all=true for read+unread.",
    {
        "type": "object",
        "properties": {"all": {"type": "boolean"}},
        "required": [],
    },
)
async def github_list_notifications(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.list_notifications(bool(args.get("all", False))),
        retryable_read=True, service="GitHub",
    )


@tool(
    "github_search",
    (
        "GitHub /search/issues query. This endpoint indexes issues & PRs only — "
        "supported qualifiers include `repo:owner/name`, `is:pr`/`is:issue`, "
        "`is:open`, `author:`, `label:`, `in:title`/`in:body`, and free-text words. "
        "It does NOT support code/branch qualifiers like `head:` or `base:` (→ HTTP 422). "
        "To find the PR for a branch, search free-text on the ticket key "
        "(e.g. `repo:owner/name is:pr KRP-1234 in:title`); resolve `owner/name` via "
        "`list_services` first — never guess the slug."
    ),
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "issue-search syntax; no head:/base: qualifiers"},
            "kind": {"type": "string", "description": "label for the result section"},
        },
        "required": ["query"],
    },
)
async def github_search(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.search(args["query"], args.get("kind", "search")),
        retryable_read=True, service="GitHub",
    )


@tool(
    "github_get_pr",
    "Fetch a PR's title, body, state, head/base, author. Use when the user references a PR number or link.",
    {
        "type": "object",
        "properties": {
            "pr": {"type": "integer"},
            "repo": {
                "type": "string",
                "description": (
                    "exact `owner/name` slug (e.g. `GoGoXTech/order-service`) — NOT a "
                    "loose service name. Resolve it via `list_services` first; never "
                    "guess the owner/org."
                ),
            },
        },
        "required": ["pr"],
    },
)
async def github_get_pr(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.get_pr(args["pr"], args.get("repo")),
        retryable_read=True, service="GitHub",
    )


@tool(
    "github_get_pr_diff",
    "Fetch a PR's unified diff, truncated to max_chars (default 20000)." + _ARCH_HINT,
    {
        "type": "object",
        "properties": {
            "pr": {"type": "integer"},
            "repo": {
                "type": "string",
                "description": (
                    "exact `owner/name` slug (e.g. `GoGoXTech/order-service`) — NOT a "
                    "loose service name. Resolve it via `list_services` first; never "
                    "guess the owner/org."
                ),
            },
            "max_chars": {"type": "integer"},
        },
        "required": ["pr"],
    },
)
async def github_get_pr_diff(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: github_int.get_pr_diff(
            args["pr"], args.get("repo"), int(args.get("max_chars", 20000)),
        ),
        retryable_read=True, service="GitHub",
    )


# ============================================================================
# Jira (10)
# ============================================================================

@tool(
    "jira_list_my_issues",
    "List Jira issues assigned to the bot user.",
    {
        "type": "object",
        "properties": {"state": {"type": "string"}},
        "required": [],
    },
)
async def jira_list_my_issues(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.list_my_issues(args.get("state", "open")),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_list_my_in_progress",
    "List the bot user's in-progress Jira issues.",
    {"type": "object", "properties": {}, "required": []},
)
async def jira_list_my_in_progress(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.list_my_in_progress(),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_list_my_sprint",
    "List the bot user's issues in the active sprint, optionally filtered by status.",
    {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": [],
    },
)
async def jira_list_my_sprint(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.list_my_sprint(args.get("status")),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_list_project_in_progress",
    "List in-progress Jira issues across a project.",
    {
        "type": "object",
        "properties": {"project": {"type": "string"}},
        "required": [],
    },
)
async def jira_list_project_in_progress(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.list_project_in_progress(args.get("project")),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_get_issue",
    "Fetch a Jira issue by key (ABC-123).",
    {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    },
)
async def jira_get_issue(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.get_issue(args["key"]),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_get_comments",
    "Read the latest comments on a Jira issue (default 5). Accepts a key "
    "(ABC-123) or a browse URL. Use when the user asks about discussion/comments "
    "on a ticket — get_issue returns only the description, not comments.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "limit": {
                "type": "integer",
                "description": "How many recent comments to fetch (default 5, max 20).",
            },
        },
        "required": ["key"],
    },
)
async def jira_get_comments(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.get_comments(args["key"], args.get("limit", 5)),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_search",
    "Run a JQL search. Use full Jira JQL in `jql`.",
    {
        "type": "object",
        "properties": {
            "jql": {"type": "string"},
            "max_results": {"type": "integer"},
            "kind": {"type": "string"},
        },
        "required": ["jql"],
    },
)
async def jira_search(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.search_jql(
            args["jql"], int(args.get("max_results", 20)),
            args.get("kind", "Results"),
        ),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_create_issue",
    "Create a new Jira issue. `description` is business/functional content in "
    "English — problem, scenarios/cases (Given/When/Then), acceptance criteria — "
    "NOT code or implementation detail; use blank lines for paragraphs, `- ` for "
    "bullets, `# ` for headings. Pass `mentions` to @-notify people (accountId + "
    "name); resolve unknowns with jira_search_users.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "description": {"type": "string"},
            "project": {"type": "string"},
            "issue_type": {"type": "string"},
            "mentions": {
                "type": "array",
                "description": "People to @-mention in the ticket body (cc line).",
                "items": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["account_id"],
                },
            },
        },
        "required": ["summary"],
    },
)
async def jira_create_issue(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.create_issue(
            args["summary"], args.get("description", ""),
            args.get("project"), args.get("issue_type", "Task"),
            args.get("mentions"),
        ),
        retryable_read=False, service="Jira",
    )


@tool(
    "jira_list_team",
    "Get the KR team roster (role → name → accountId) — use to resolve who a "
    "role means (PM / QA / tech lead / BE senior / reporter) before @-mentioning "
    "them via jira_create_issue `mentions`. People outside the roster: use "
    "jira_search_users.",
    {"type": "object", "properties": {}},
)
async def jira_list_team(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.list_team(), retryable_read=True, service="Jira",
    )


@tool(
    "jira_search_users",
    "Search Jira users by name or email → accountId, for @-mentions or assignment "
    "when a person is not in the brain's built-in team roster.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Name or email fragment."},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    },
)
async def jira_search_users(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.search_users(args["query"], args.get("max_results", 10)),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_comment_issue",
    "Post a comment on a Jira issue.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["key", "body"],
    },
)
async def jira_comment_issue(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.comment_issue(args["key"], args["body"]),
        retryable_read=False, service="Jira",
    )


@tool(
    "jira_assign_issue",
    "Assign a Jira issue to a user. Omit `assignee` (or pass 'me') to assign to "
    "the bot's own Jira account; otherwise pass an email or display name. Needed "
    "before transitions that require an assignee (e.g. Code Review).",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "assignee": {
                "type": "string",
                "description": "Email/display name; empty or 'me' = the API-token user.",
            },
        },
        "required": ["key"],
    },
)
async def jira_assign_issue(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.assign_issue(args["key"], args.get("assignee")),
        retryable_read=False, service="Jira",
    )


@tool(
    "jira_list_transitions",
    "List available workflow transitions for a Jira issue.",
    {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    },
)
async def jira_list_transitions(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.list_transitions(args["key"]),
        retryable_read=True, service="Jira",
    )


@tool(
    "jira_transition_issue",
    "Move a Jira issue to a target status name (case-insensitive match against transition name or destination status).",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "target_status": {"type": "string"},
        },
        "required": ["key", "target_status"],
    },
)
async def jira_transition_issue(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: jira_int.transition_issue(args["key"], args["target_status"]),
        retryable_read=False, service="Jira",
    )


# ============================================================================
# Git (5)
# ============================================================================

@tool(
    "list_services",
    "List the registered service repos (name, github_repo `owner/name`, local "
    "repo_path, aliases). Call this to resolve a service the user names loosely "
    "(e.g. 'order', 'payment') to its exact `owner/name` BEFORE using github/git "
    "tools — never guess the owner/repo slug.",
    {"type": "object", "properties": {}, "required": []},
)
async def list_services(args: dict[str, Any]) -> dict[str, Any]:
    import json as _json
    rows = _list_services()
    if not rows:
        return _ok("(service registry empty)")
    lines = []
    for s in rows:
        try:
            aliases = _json.loads(s.get("aliases") or "[]")
        except (ValueError, TypeError):
            aliases = []
        alias_s = f" (aliases: {', '.join(aliases)})" if aliases else ""
        lines.append(
            f"- {s.get('name')} → github: {s.get('github_repo')} · "
            f"path: {s.get('repo_path')}{alias_s}"
        )
    return _ok(f"Service registry ({len(rows)}):\n" + "\n".join(lines))


@tool(
    "git_check_repo",
    "Inspect a local service repo: current branch, uncommitted changes, remotes. Supply either service (from service_repos) or repo path.",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "repo": {"type": "string"},
        },
        "required": [],
    },
)
async def git_check_repo(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: git_int.check_repo(args.get("service"), args.get("repo")),
        retryable_read=True, service="git",
    )


@tool(
    "git_latest_release",
    "Fetch fresh remote state (via GITHUB_TOKEN over HTTPS, no SSH) and report the "
    "most recent releases/* branch and its HEAD commit (full+short id, message, author, "
    "date). Use this for any 'what's the latest release branch / commit id' question — "
    "do NOT run a raw `git fetch` via Bash, which uses the SSH remote and fails in the "
    "sandbox. Supply either service (from service_repos) or repo path.",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "repo": {"type": "string"},
        },
        "required": [],
    },
)
async def git_latest_release(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: git_int.latest_release_branch(args.get("service"), args.get("repo")),
        retryable_read=True, service="git",
    )


@tool(
    "git_prepare_read_workspace",
    "Fetch fresh remote code (via GITHUB_TOKEN over HTTPS, no SSH) and check it out into a "
    "dedicated READ worktree, returning its path. Use this BEFORE grep/read/trace of a "
    "service's code — the main local clone may sit on a stale branch (e.g. an old `master`), "
    "so grepping it reads outdated code. Defaults to the latest `releases/*` branch; pass "
    "`ref` (e.g. a specific `releases/DAPro-2.47` or any branch) to pin another. Grep/read "
    "the returned `read_path`, not the main clone.",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "repo": {"type": "string", "description": "owner/name; alternative to service"},
            "ref": {"type": "string", "description": "Branch to check out; default = latest releases/*"},
        },
        "required": [],
    },
)
async def git_prepare_read_workspace(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: git_int.prepare_read_workspace(
            args.get("service"), args.get("repo"), args.get("ref")
        ),
        retryable_read=False, service="git",
    )


@tool(
    "git_prepare_workspace",
    "Create a worktree + feature/<ticket> branch for a service. Base auto-resolved from Jira sprint (releases/DAPro-2.<sprint>).",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "ticket": {"type": "string", "description": "Jira key ABC-123"},
        },
        "required": ["service", "ticket"],
    },
)
async def git_prepare_workspace(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: git_int.prepare_workspace(args["service"], args["ticket"], confirmed=True),
        retryable_read=False, service="git",
    )


@tool(
    "git_prepare_pr_review_workspace",
    "Clone/checkout a PR head into a review worktree so review agents can inspect files.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "pr": {"type": "integer"},
        },
        "required": ["repo", "pr"],
    },
)
async def git_prepare_pr_review_workspace(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: git_int.prepare_pr_review_workspace(args["repo"], int(args["pr"])),
        retryable_read=False, service="git",
    )


@tool(
    "git_commit",
    "Stage and commit all changes in the worktree for service/feature-<ticket>.",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "ticket": {"type": "string", "description": "branch suffix after feature/"},
            "message": {"type": "string"},
        },
        "required": ["service", "ticket", "message"],
    },
)
async def git_commit(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: git_int.commit_branch(
            args["service"], args["ticket"], args["message"], confirmed=True,
        ),
        retryable_read=False, service="git",
    )


@tool(
    "git_push",
    "Push feature/<ticket> to origin. Uses GITHUB_TOKEN auth, no SSH required.",
    {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "ticket": {"type": "string", "description": "branch suffix after feature/"},
        },
        "required": ["service", "ticket"],
    },
)
async def git_push(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: git_int.push_branch(args["service"], args["ticket"]),
        retryable_read=False, service="git",
    )


# ============================================================================
# Notion (4) — create / read / update / delete
# ============================================================================

@tool(
    "notion_create_page",
    "Create a Notion page (markdown body) under a parent page. Use to publish "
    "docs / notes / analysis to Notion. `parent` defaults to NOTION_PARENT_PAGE_ID. "
    "Write tool — no retry.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "markdown": {"type": "string", "description": "page body in markdown"},
            "parent": {"type": "string", "description": "Notion parent page id or URL; defaults to NOTION_PARENT_PAGE_ID"},
        },
        "required": ["title"],
    },
)
async def notion_create_page(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: notion_int.create_page(
            args["title"], args.get("markdown", ""), args.get("parent"),
        ),
        retryable_read=False, service="Notion",
    )


@tool(
    "notion_get_page",
    "Read a Notion page by id or URL — returns its title + body as markdown. "
    "Requires the Notion integration to be shared on that page (else NOT_FOUND). "
    "Read-only — no confirm.",
    {
        "type": "object",
        "properties": {
            "page": {"type": "string", "description": "Notion page id or URL"},
        },
        "required": ["page"],
    },
)
async def notion_get_page(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: notion_int.get_page(args["page"]),
        retryable_read=True, service="Notion",
    )


@tool(
    "notion_update_page",
    "Update a Notion page (id or URL): rename via `title` and/or write body via "
    "`markdown`. `replace_body=true` archives existing content first (full "
    "rewrite); otherwise the markdown is appended to the end. Give at least one of "
    "title/markdown. Write tool — no retry.",
    {
        "type": "object",
        "properties": {
            "page": {"type": "string", "description": "Notion page id or URL"},
            "title": {"type": "string", "description": "new page title (optional)"},
            "markdown": {"type": "string", "description": "body in markdown to append (or replace)"},
            "replace_body": {"type": "boolean", "description": "true = clear existing body before writing (default false = append)"},
        },
        "required": ["page"],
    },
)
async def notion_update_page(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: notion_int.update_page(
            args["page"], args.get("title"), args.get("markdown"),
            bool(args.get("replace_body", False)),
        ),
        retryable_read=False, service="Notion",
    )


@tool(
    "notion_delete_page",
    "Delete (archive) a Notion page by id or URL. Reversible — pass `restore=true` "
    "to un-archive. Write tool — no retry.",
    {
        "type": "object",
        "properties": {
            "page": {"type": "string", "description": "Notion page id or URL"},
            "restore": {"type": "boolean", "description": "true = restore instead of delete (default false)"},
        },
        "required": ["page"],
    },
)
async def notion_delete_page(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: notion_int.archive_page(args["page"], bool(args.get("restore", False))),
        retryable_read=False, service="Notion",
    )


# ============================================================================
# Grafana (2)
# ============================================================================

@tool(
    "grafana_search_logs",
    (
        "Query Loki via Grafana for logs. Provide either `query` (full LogQL) or "
        "`service` + optional `filter` (line filter expression like `|= \"error\"` "
        "or `|~ \"(?i)error|exception\"`). Times use Grafana shorthand "
        "(`now`, `now-1h`, `now-30m`). Read-only — no confirm."
    ),
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "raw LogQL — overrides service+filter"},
            "env": {"type": "string", "enum": ["stag", "prod"]},
            "since": {"type": "string"},
            "until": {"type": "string"},
            "limit": {"type": "integer", "description": "max log lines (default 100); raise only if you truly need more — every returned line stays in the thread transcript and is re-read each later turn"},
            "direction": {"type": "string", "enum": ["backward", "forward"]},
            "datasource_uid": {"type": "string"},
            "service": {"type": "string"},
            "filter": {"type": "string", "description": "LogQL line filter, e.g. |= \"error\""},
        },
        "required": [],
    },
)
async def grafana_search_logs(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: grafana_int.search_logs(
            query=args.get("query", ""),
            env=args.get("env", "stag"),
            since=args.get("since", "now-1h"),
            until=args.get("until", "now"),
            limit=int(args.get("limit", 100)),
            direction=args.get("direction", "backward"),
            datasource_uid=args.get("datasource_uid"),
            service=args.get("service", ""),
            log_filter=args.get("filter", ""),
        ),
        retryable_read=True, service="Grafana", retry_timeout=False,
    )


@tool(
    "grafana_list_datasources",
    "List Grafana datasources for an environment. Use to find a Loki datasource UID.",
    {
        "type": "object",
        "properties": {"env": {"type": "string", "enum": ["stag", "prod"]}},
        "required": [],
    },
)
async def grafana_list_datasources(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: grafana_int.list_datasources(args.get("env", "stag")),
        retryable_read=True, service="Grafana",
    )


# ============================================================================
# Staging DB (1) — read-only introspection via the order-service debug API
# ============================================================================

@tool(
    "db_query",
    (
        "Run ONE read-only SQL statement against the ggx-kr-order-service STAGING "
        "read replica via its admin debug-query API and return the rows. Use this "
        "for the *current* schema and config-in-DB values — live introspection is "
        "the source of truth, the legacy `db/schema.rb` is stale. "
        + _DB_SCHEMA_HINT +
        "Allowed: SELECT / WITH / SHOW / DESCRIBE / EXPLAIN only; mutations, "
        "multi-statements and file access are rejected (client + server guard). "
        "Examples: `SHOW CREATE TABLE orders`, `DESCRIBE orders`, "
        "`SELECT id, status FROM orders WHERE user_id=12345`. "
        "A bare SELECT without LIMIT is capped automatically. Staging read replica, "
        "read-only — no confirm. (Temporary tool — staging only.)"
    ),
    {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "one read-only statement"},
        },
        "required": ["sql"],
    },
)
async def db_query(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: db_int.query(args.get("sql", "")),
        retryable_read=True, service="DB",
    )


# ============================================================================
# Production DB (1) — read-only, CONFIRM-gated (real customer PII)
# ============================================================================

@tool(
    "db_query_prod",
    (
        "Run ONE read-only SQL statement against the ggx-kr-order-service "
        "PRODUCTION read replica via its admin debug-query API and return the rows. "
        "This hits REAL customer data (PII) and every call is audit-logged, so it "
        "ALWAYS asks the user for a Slack confirm button before running — the "
        "confirmation is handled for you; do NOT ask again in text. "
        "Prefer db_query (staging) whenever the data exists on staging; only reach "
        "for prod to diagnose a live incident, verify prod-only records, or confirm "
        "a data fix. Filter tightly — never `SELECT *` on broad tables. "
        + _DB_SCHEMA_HINT +
        "Allowed: SELECT / WITH / SHOW / DESCRIBE / EXPLAIN only; mutations, "
        "multi-statements and file access are rejected (client + server guard). "
        "A bare SELECT without LIMIT is capped automatically."
    ),
    {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "one read-only statement"},
        },
        "required": ["sql"],
    },
)
async def db_query_prod(args: dict[str, Any]) -> dict[str, Any]:
    # Read-only + CONFIRM-gated: the can_use_tool callback already got a human
    # yes before this body runs, so retrying transient errors is safe (no side
    # effect); each retry does re-hit prod + audit log, kept to _MAX_RETRIES.
    return await _run_with_retry(
        lambda: db_int.query_prod(args.get("sql", "")),
        retryable_read=True, service="DB(prod)",
    )


# ============================================================================
# Ship (1)
# ============================================================================

@tool(
    "ship_create_pr",
    (
        "Open a PR from an existing local worktree (service+ticket). Auto-resolves base "
        "from Jira sprint (releases/DAPro-2.<sprint>) and transitions the Jira issue to "
        "Code Review on success. Prefer this over github_create_pr when a worktree exists."
    ),
    {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "ticket": {"type": "string"},
            "pr_title": {"type": "string"},
            "commit_message": {"type": "string"},
            "pr_body": {"type": "string"},
            "base": {"type": "string"},
            "target_status": {"type": "string"},
            "draft": {"type": "boolean"},
        },
        "required": ["service", "ticket", "pr_title"],
    },
)
async def ship_create_pr(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_retry(
        lambda: ship_int.create_pr(
            args["service"], args["ticket"], args["pr_title"],
            args.get("commit_message", ""), args.get("pr_body", ""),
            args.get("base"),
            args.get("target_status", "Code Review"),
            bool(args.get("draft", False)),
        ),
        retryable_read=False, service="ship",
    )


# ============================================================================
# Aggregator
# ============================================================================

_ALL_TOOLS = [
    # github (14)
    github_create_issue, github_create_pr, github_comment_pr,
    github_add_assignees,
    github_approve_pr, github_merge_pr, github_update_pr,
    github_list_my_prs, github_list_prs, github_list_issues,
    github_list_notifications, github_search,
    github_get_pr, github_get_pr_diff,
    # jira (14)
    jira_list_my_issues, jira_list_my_in_progress, jira_list_my_sprint,
    jira_list_project_in_progress, jira_get_issue, jira_get_comments, jira_search,
    jira_create_issue, jira_comment_issue, jira_assign_issue,
    jira_list_transitions, jira_transition_issue, jira_search_users, jira_list_team,
    # registry (1)
    list_services,
    # git (6)
    git_check_repo, git_latest_release, git_prepare_read_workspace,
    git_prepare_workspace,
    git_prepare_pr_review_workspace, git_commit, git_push,
    # notion (4)
    notion_create_page, notion_get_page, notion_update_page, notion_delete_page,
    # grafana (2)
    grafana_search_logs, grafana_list_datasources,
    # staging db (1)
    db_query,
    # prod db (1) — CONFIRM-gated
    db_query_prod,
    # ship (1)
    ship_create_pr,
]


def build_agentic_mcp_server():
    """Return the in-process MCP server config for ``ClaudeAgentOptions.mcp_servers``."""
    return create_sdk_mcp_server(
        name="agentic",
        version="0.2.0",
        tools=_ALL_TOOLS,
    )
