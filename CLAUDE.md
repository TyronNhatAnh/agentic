# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Slack bot (Socket Mode, `slack-bolt` async) that routes user messages to a long-lived Claude Agent SDK session per thread. The brain (Claude with a system prompt + in-process MCP tools + sub-agents) decides intent, calls tools, and delegates work natively — Python is the tool runtime + safety boundary, not an orchestrator. The bot depends on a working `claude login` on the host (no API key; `claude --version` must be ≥ `MIN_CLAUDE_VERSION`). Prompts and user-facing strings are in English; the brain mirrors the user's language (English default, may reply in Vietnamese or Korean).

> **Architecture.** One `ClaudeSDKClient` per Slack thread ([sdk/brain_session.py](src/agentic/sdk/brain_session.py)), pooled by `ThreadSessionManager` ([sdk/client_pool.py](src/agentic/sdk/client_pool.py)) with idle-TTL eviction, resumed across restarts via `threads.sdk_session_id`. Sub-agents (dev/review/ba/po) are `AgentDefinition` entries in the same session ([sdk/sub_agents.py](src/agentic/sdk/sub_agents.py)), reached through the native `Task` tool. Integrations are in-process MCP `@tool`s ([sdk/mcp_tools.py](src/agentic/sdk/mcp_tools.py)). Permission, hooks, and observability are described below. The SDK migration is complete — there is no `claude -p` subprocess path and no `AGENTIC_USE_SDK` flag. History lives in [MIGRATION_PLAN.md](MIGRATION_PLAN.md).

## Common commands

Use the Makefile — do not invent equivalents.

- `make install` — venv + editable install + copy `.env.example` to `.env`
- `make run` / `make debug` — foreground (debug sets `LOG_LEVEL=DEBUG`)
- `make start` / `make stop` / `make restart` / `make status` / `make logs` — background process via `.agentic.pid` + `agentic.log`
- `make test` — runs `pytest -q` (pytest-asyncio in `auto` mode, testpaths=`tests`)
- Single test: `.venv/bin/pytest tests/test_dispatcher.py::test_name -q`
- `make db-show` — last 20 `runs` rows · `make db-stats` — cache_read ratio / cost-per-thread / tool fail rate · `make db-reset` — wipe `agentic.db`

Required env (see [.env.example](.env.example)): `SLACK_BOT_TOKEN` (xoxb-), `SLACK_APP_TOKEN` (xapp-, Socket Mode). Optional: `GITHUB_TOKEN` + `GITHUB_DEFAULT_REPO`, `JIRA_*`, `SLACK_ALLOWED_CHANNELS` (comma-separated channel names; empty = all allowed), `WORKER_CONCURRENCY` (default 4), `CLAUDE_BIN`, `WORKSPACE_DIR`/`WORKTREE_DIR` (where git work clones/checkouts land — must be set if you use git actions), `AGENTIC_SERVICES_JSON` (path to a JSON list of service repo seeds — schema: `[{name, repo_path, github_repo, base_branch_template?, jira_board_id?, aliases?}]`), `SDK_SESSION_IDLE_TTL_S` (default 1800), `SDK_MAX_CONCURRENT_SESSIONS` (default 20), `MIN_CLAUDE_VERSION`.

## Request lifecycle

1. [slack_handlers.py](src/agentic/slack_handlers.py) receives `app_mention` (DMs are intentionally ignored), checks the channel allowlist via cached `conversations_info`, fetches the thread via `conversations.replies` (so non-mention user messages — e.g. a teammate posting a PR link — are visible to the brain), posts a "Đang xử lý..." placeholder, and submits a `Job` (with `thread_history`, `slack_client`, `placeholder_ts`) to the `JobRunner`. It also registers the `perm_allow`/`perm_deny` button handlers.
2. [worker.py](src/agentic/worker.py) `JobRunner` is an in-process async queue with N workers and a per-`thread_ts` busy set — concurrent requests in the same thread are rejected rather than queued. It forwards `slack_client` + `placeholder_ts` to the handler.
3. [dispatcher.py](src/agentic/dispatcher.py) `handle_message` (~290 LoC) is thin:
   - resolves any worktree already prepared for the ticket in play (`_resolve_active_workspace`) and persists `active_ticket`/`active_worktree`/`repo` to the thread row, so a workspace hint can route fix/PR work to the dev sub-agent;
   - delegates the whole turn to `run_brain_session` ([sdk/brain_session.py](src/agentic/sdk/brain_session.py)) — gets/creates the thread's `ClaudeSDKClient`, sends the user message (with Slack `thread_history` + workspace hint injected into the **user message only**, never the system prompt, to keep the cache prefix stable), and streams the brain's reply into the Slack placeholder (debounced ~1.5s, with `chat.update` 429/Retry-After backoff);
   - logs one `brain` row to `runs` carrying the session usage/cost (per-tool rows are written by hooks, see below);
   - returns the final reply text + a `🛠️ N tool · Xs · tok · $cost` footer.
4. The worker edits the placeholder in place with the returned reply (final flush), chunking long messages.

The streamed partial text **is** the progress indicator — there is no separate progress *message*, and the `Job` carries no `progress` callback. Two writers share that one placeholder: the stream loop, and a `_heartbeat` task that appends an elapsed-time line when the model stalls before emitting anything (a turn once sat 344s before the first token, leaving the placeholder frozen). Both go through `_Progress`, which owns the edit slot — a lock so they can't interleave `chat.update` on the same message, plus the shared debounce/429 backoff. Any new writer must go through it too, not straight to `chat.update`.

The brain emits native `tool_use` blocks; the SDK validates input against each `@tool`'s typed schema and runs it. There is **no** JSON-from-stdout parsing and no Python ReAct loop — the SDK orchestrates tool calls and sub-agent delegation.

## Permission / confirmation flow for destructive actions

`github_merge_pr` (the `CONFIRM_TOOLS` set, [sdk/permission.py](src/agentic/sdk/permission.py)) requires user confirmation. `github_approve_pr` deliberately does not — the review flow auto-LGTMs a PR whose verdict is APPROVE. The mechanism is a `can_use_tool` callback, **not** text parsing:

- The brain calls the tool → the callback (`build_slack_permission_callback`) checks the whitelist, posts a Slack message with ✅/❌ block-kit buttons, creates an in-memory `asyncio.Future` keyed by `req_id`, and `await`s it (timeout 5' → auto-Deny).
- The user clicks a button → the `perm_allow`/`perm_deny` action handler calls `pending.resolve(req_id, allow)`, which completes the Future → the callback returns `PermissionResultAllow/Deny`. The SDK session blocks in its own async context, so nothing is persisted across turns.
- A text reply (instead of a button click) falls through to the brain as a normal message; the pending Future keeps waiting for a button or timeout. We do **not** parse yes/no from text.
- `github_merge_pr` re-checks `mergeable_state` ∈ `{clean, unstable}` inside the tool body before the side effect; bad states return a `VALIDATION` error for the brain to relay. The brain prompt must **not** ask its own confirm — the callback owns it.

Tools that aren't in `CONFIRM_TOOLS` and aren't in any `allowed_tools` list still fire the callback and are auto-allowed. (The SDK skips `can_use_tool` for tools already in `allowed_tools` — so gating an allow-listed tool requires a `PreToolUse` hook, not the callback.)

## Hooks (audit + observability)

[sdk/hooks.py](src/agentic/sdk/hooks.py) `build_brain_hooks(thread_ts, channel)` is wired into the brain options factory per thread:

- `PreToolUse` — stamps a monotonic start keyed by `tool_use_id` (audit), and **denies** raw network git in Bash (`git fetch`/`git pull` without an `https://` URL) — the SSH remote has no key in the bot's env, so those either fail or leave stale refs that later reads report as current; the deny reason steers the brain to `git_latest_release` / token-URL fetch.
- `PostToolUse` / `PostToolUseFailure` — single-writer for per-tool `runs` rows (ok / error + duration). PostToolUse only fires on success, so failures need their own hook. Secret tokens (`ghp_…`, `xox?-…`, `x-access-token:…`, `//user:pass@…`) are redacted from the logged input preview — audit-only, the tool input itself is not mutated.
- `PreCompact` — `log.warning(trigger=...)`. The SDK fires this when compaction is already happening (auto-compaction handles long threads natively); it is not an "almost full" forecast and posts nothing to Slack.

## Adding a sub-agent

Add an entry to `build_subagents()` in [sdk/sub_agents.py](src/agentic/sdk/sub_agents.py): an `AgentDefinition` with `description`, `prompt=load_prompt("<name>")`, `tools` (subset; use `mcp__agentic__<tool>` for MCP tools and pass `mcpServers=["agentic"]`), `model`, and optional `permissionMode`/`disallowedTools`. Drop a `prompts/<name>.md`, and mention when to pick the role in [prompts/brain_sdk.md](src/agentic/prompts/brain_sdk.md) (the `description` is WHAT the agent does — auto-injected into the `Task` tool schema; the brain prose is WHEN to use it). Prompt strings + tool lists are read once at process start so the session prefix cache stays warm. Current roles: po/ba (text-only, `tools=[]`), review (Read/Glob/Grep + `github_get_pr{,_diff}` + `git_prepare_pr_review_workspace` + read-only `db_query{,_prod}`/`grafana_search_logs` so it can size a finding against real data — deliberately **no Bash**, so no `git log/diff/show` and no way to mutate), dev (`acceptEdits`, full git/gh via Bash, force-push/reset blocked via `disallowedTools`).

## Adding an integration action

Extend the relevant `integrations/*.py` `execute_action(type, payload)` dispatcher and return a `ToolResult` (see [integrations/result.py](src/agentic/integrations/result.py)). Then expose it as a `@tool` in [sdk/mcp_tools.py](src/agentic/sdk/mcp_tools.py) with a typed `input_schema` (1-1 with the legacy `<integration>.<verb>` action; tool name is the action type with `.`→`_`, e.g. `github_get_pr`), include it in `build_agentic_mcp_server()`, and reference it as `mcp__agentic__<tool_name>` in any agent's `tools` allowlist. Wrap the call in `_run_with_retry(retryable_read=...)`: read-only tools retry transient errors (TIMEOUT/NETWORK/SERVER/RATE_LIMIT) 2× with backoff; write tools never retry (a timed-out POST may have succeeded). `error_code` values `AUTH`/`CONFIG`/`VALIDATION`/`NOT_FOUND` are user-facing; internal/transport codes are returned to the brain as `{"ok": false, ...}` for it to relay. If a tool has user-visible side effects and shouldn't auto-run, add it to `CONFIRM_TOOLS`.

## Design principle: let the model reason

This bot is powered by a capable model; do not turn it into a brittle rule engine.

- Prefer passing high-quality thread context, tool outputs, and structured state to the brain/agents so the model can decide intent, continuity, and whether a request was already handled.
- Keep Python code focused on deterministic plumbing: Slack delivery, job orchestration, retries, persistence, tool execution, output formatting, and hard safety/validation boundaries.
- Avoid overfitting user intent with hard-coded phrase checks such as "if the user says X, always do Y" unless it protects a real integration boundary or prevents a concrete failure. Match SDK tool descriptors (`tool_name`, `tool_input`) for permission, not user message text.
- If behavior seems wrong, first improve the context/prompt contract or agent handoff. Add rules only when the decision is truly deterministic and cheaper/safer outside the model.

## Persistence

SQLite at `AGENTIC_DB` (default `agentic.db`). Schema in [store.py](src/agentic/store.py):
- `runs` — every brain/tool invocation. The `brain` row carries observability columns (`cache_read_input_tokens`, `cache_creation_input_tokens`, `input_tokens`, `output_tokens`, `cost_usd`, `num_turns`) filled from `ResultMessage.usage`; tool rows (written by hooks) leave them null. `init_db()` migrates older DBs via `_RUNS_ADDED_COLUMNS` (mirror of the thread-column migration). `make db-stats` reads these.
- `threads` — one row per Slack thread: `repo`, `active_ticket`, `active_worktree`, plus `sdk_session_id` (resume token) so the brain session survives a process restart. `init_db()` migrates older DBs by `ALTER TABLE ADD COLUMN`, so adding a thread field means updating `SCHEMA` and `_THREAD_ADDED_COLUMNS`/`_THREAD_FIELDS`. (The legacy `sdk_state_blob` column is retained read-only for one-time fallback — see `session_entries`.)
- `session_entries` — the SDK session transcript, append-only ([sdk/session_store.py](src/agentic/sdk/session_store.py) via `append_session_entries`/`load_session_entries`). One row per entry keyed by `(thread_ts, sdk_session_id)`, so an append is O(new entries) and atomic per-transaction — replacing the read-modify-write of `threads.sdk_state_blob` that rewrote the whole transcript each turn and could corrupt on a mid-write crash. One live session per thread; rows for a superseded `session_id` are pruned on the next append. `load` falls back to the legacy blob once. Subagent transcripts (keys with a `subpath`) are not persisted.
- `messages` — assistant-side chat history; durability/fallback for `recent_messages()` when a Slack `conversations.replies` fetch returns nothing. Live context is the SDK session + Slack history.
- `service_repos` — local service registry seeded from `AGENTIC_SERVICES_JSON` on startup.

Confirmation state is in-memory only (`PendingPermissions` Futures) — there is no `pending_confirmations` table.

## Testing notes

`tests/` is hermetic — no live `claude`/Slack/GitHub calls. Coverage: permission Future semantics + callback flow ([test_sdk_phase1.py](tests/test_sdk_phase1.py)), hooks + observability columns ([test_sdk_phase4.py](tests/test_sdk_phase4.py)), brain options/sub-agent shape, thread-history rendering ([test_brain_routing.py](tests/test_brain_routing.py)), service resolution + brain delegation ([test_dispatcher.py](tests/test_dispatcher.py)), MCP build + session caching/eviction ([test_sdk_smoke.py](tests/test_sdk_smoke.py)). When testing the dispatcher, monkeypatch `run_brain_session` + the `_brain_pool_singleton`/`_pending_singleton` getters rather than the SDK transport.
