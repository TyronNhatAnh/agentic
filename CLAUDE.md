# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Slack bot (Socket Mode, `slack-bolt` async) that routes user messages to specialized role-based sub-agents. Each "agent" is a subprocess call to the local `claude` CLI (`claude -p ... --system-prompt ...`) with a role-specific markdown prompt from [src/agentic/prompts/](src/agentic/prompts/). The bot itself depends on a working `claude login` on the host. Default prompt language is Vietnamese.

## Common commands

Use the Makefile — do not invent equivalents.

- `make install` — venv + editable install + copy `.env.example` to `.env`
- `make run` / `make debug` — foreground (debug sets `LOG_LEVEL=DEBUG`)
- `make start` / `make stop` / `make restart` / `make status` / `make logs` — background process via `.agentic.pid` + `agentic.log`
- `make test` — runs `pytest -q` (pytest-asyncio in `auto` mode, testpaths=`tests`)
- Single test: `.venv/bin/pytest tests/test_dispatcher.py::test_name -q`
- `make db-show` / `make db-reset` — inspect or wipe `agentic.db`

Required env (see [.env.example](.env.example)): `SLACK_BOT_TOKEN` (xoxb-), `SLACK_APP_TOKEN` (xapp-, Socket Mode). Optional: `GITHUB_TOKEN` + `GITHUB_DEFAULT_REPO`, `JIRA_*`, `SLACK_ALLOWED_CHANNELS` (comma-separated channel names; empty = all allowed), `WORKER_CONCURRENCY` (default 4), `CLAUDE_BIN`/`CLAUDE_TIMEOUT`, `WORKSPACE_DIR`/`WORKTREE_DIR` (where git agents clone/checkout — must be set if you use git actions), `AGENTIC_SERVICES_JSON` (path to a JSON list of service repo seeds — schema: `[{name, repo_path, github_repo, base_branch_template?, jira_board_id?, aliases?}]`).

## Request lifecycle

1. [slack_handlers.py](src/agentic/slack_handlers.py) receives `app_mention` (DMs are intentionally ignored), checks channel allowlist via cached `conversations_info`, fetches the thread via `conversations.replies` (so non-mention user messages — e.g. a teammate posting a PR link — are visible to the brain), posts a "Đang xử lý..." placeholder, and submits a `Job` (with `thread_history`) to the `JobRunner`.
2. [worker.py](src/agentic/worker.py) `JobRunner` is an in-process async queue with N workers and a per-`thread_ts` busy set — concurrent requests in the same thread are rejected (`_BUSY_MSG`) rather than queued.
3. [dispatcher.py](src/agentic/dispatcher.py) `handle_message` is the orchestrator:
   - prefers `thread_history` from Slack (passed via the `Job`); falls back to `recent_messages` from SQLite if empty,
   - calls [brain.py](src/agentic/brain.py) `decide()` which runs `claude -p` with [prompts/brain.md](src/agentic/prompts/brain.md) and parses a JSON response with shape `{reply, need_clarification, clarify_question, steps:[{agent,task}], actions:[{type,payload}]}`,
   - runs each `step.agent` via `REGISTRY` ([agents/__init__.py](src/agentic/agents/__init__.py)), passing the previous step's output as `context`,
   - runs each `action` through [integrations/github.py](src/agentic/integrations/github.py) or [integrations/jira.py](src/agentic/integrations/jira.py) with retry (`_MAX_RETRIES=2`, only when `ToolResult.retryable` **and** the action is read-only; write actions — `github.create_*`/`github.comment_*`/`github.approve_*`/`github.merge_*`/`jira.create_*`/`jira.comment_*`/`jira.transition_*`/`git.*` — are never retried to avoid duplicates),
   - logs every step into the `runs` table and schedules a thread summary via [summarizer.py](src/agentic/summarizer.py).
4. The placeholder Slack message is edited in place with the rendered result. Non-review replies are passed through `_shrink_reply` (cap `_REPLY_SAFE_LEN = 2500` chars, summarized by `claude -p` if longer) and chunked at `_SLACK_CHUNK_LEN = 3500` chars per Slack message.

## Confirmation flow for destructive actions

`github.approve_pr`, `github.merge_pr`, and the base-branch fallback path of `git.prepare_workspace` defer side effects until the user confirms. The pattern:

- First invocation: handler returns `ToolResult.failure("NEEDS_CONFIRMATION", question)` with `result.data = {"action_type": ..., "payload": {..., "confirmed": True}}`. Dispatcher persists this to the `pending_confirmations` table (TTL 30 min) and renders the question with a ❓ prefix.
- Next user message: `dispatcher._is_affirmative` / `_is_negative` match against a closed set (`ok`, `ừ`, `được`, `cancel`, ...); affirmative → `_run_pending` re-invokes the action with the saved payload (now `confirmed=True`) which executes the real side effect. Any other reply clears the pending row and falls through to the brain.
- `github.merge_pr` additionally re-checks `mergeable_state` ∈ `{clean, unstable}` on **both** the confirm prompt and the resumed execution; states like `dirty`/`blocked`/`behind`/`draft` are refused with a clear reason rather than retried.

Brain prompt must **not** ask its own confirm for these actions — orchestrator owns the confirm step.

## Agent subprocess contract

All agents and the brain share [agents/base.py](src/agentic/agents/base.py) `run_claude()`:
- Spawns `claude -p <user_prompt> --system-prompt <system_prompt> --output-format text`.
- Times out per `CLAUDE_TIMEOUT`. Non-zero exit raises `ClaudeRunError` with stderr.
- The brain's stdout must contain a JSON object; `brain._extract_json` is tolerant of code fences. Parse failures fall back to returning the raw text as `reply` (no steps/actions executed).

When adding a new agent: create `src/agentic/agents/<name>.py` that exposes `async def run_<name>(task: str, *, context: str = "") -> str`, add a `prompts/<name>.md`, and register it in `agents/__init__.py::REGISTRY`. The brain prompt must also be updated to know the new role exists.

When adding a new integration action: extend the relevant `integrations/*.py` `execute_action(type, payload)` dispatcher and return a `ToolResult` (see [integrations/result.py](src/agentic/integrations/result.py)). `error_code` values `AUTH`/`CONFIG`/`VALIDATION`/`NOT_FOUND` render as ⚠️ (non-retryable user errors); others render as ❌. Internal/transport codes (`UNKNOWN_ACTION`/`UNKNOWN`/`SERVER`/`NETWORK`/`TIMEOUT`/`RATE_LIMIT`) are rewritten by `dispatcher._USER_FRIENDLY_ERROR_MESSAGES` to a Vietnamese explanation so users don't see raw internal strings; the raw `user_message` is logged for debugging. Action `type` must use the `<integration>.<verb>` prefix — the dispatcher routes on prefix.

## Design principle: let the model reason

This bot is powered by a capable model; do not turn it into a brittle rule engine.

- Prefer passing high-quality thread context, tool outputs, and structured state to the brain/agents so the model can decide intent, continuity, and whether a request was already handled.
- Keep Python code focused on deterministic plumbing: Slack delivery, job orchestration, retries, persistence, tool execution, output formatting, and hard safety/validation boundaries.
- Avoid overfitting user intent with hard-coded phrase checks such as "if the user says X, always do Y" unless it protects a real integration boundary or prevents a concrete failure.
- If behavior seems wrong, first improve the context/prompt contract or agent handoff. Add rules only when the decision is truly deterministic and cheaper/safer outside the model.
- For repeated requests, prefer giving the brain enough recent history and summaries to answer naturally over implementing per-intent duplicate detectors.

## Persistence

SQLite at `AGENTIC_DB` (default `agentic.db`). Schema in [store.py](src/agentic/store.py):
- `runs` — every brain/agent/tool invocation (use `make db-show` to inspect).
- `threads` — one row per Slack thread, holds `summary`, `last_agent`, `jira_keys`, `pr_refs`, `repo`. `init_db()` migrates older DBs by `ALTER TABLE ADD COLUMN` on startup, so adding a new thread field requires updating both `SCHEMA` and `_THREAD_ADDED_COLUMNS`/`_THREAD_FIELDS`.
- `messages` — assistant-side chat history; durability/fallback only. Live user context now comes from Slack `conversations.replies` per request.
- `pending_confirmations` — one row per thread holding a deferred action (`action_type`, `payload`, `question`) for the confirmation flow above. TTL 30 min via `PENDING_CONFIRMATION_TTL_S`.
- `service_repos` — local service registry seeded from `AGENTIC_SERVICES_JSON` on startup.

## Testing notes

`tests/` currently covers brain JSON parsing and dispatcher orchestration with mocked agent runners; there are no live `claude`/Slack/GitHub calls. When adding tests that touch the dispatcher, mock `REGISTRY` entries and `_invoke_integration` rather than the subprocess layer.
