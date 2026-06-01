# agentic — Slack bot powered by the Claude Agent SDK

A Slack bot (Socket Mode) that routes each thread to a long-lived **Claude Agent SDK**
session. The "brain" (Claude + system prompt + in-process MCP tools + sub-agents)
decides intent, calls tools, and delegates work natively. Python is the tool runtime +
safety boundary, **not** an orchestrator.

> The bot authenticates via the host's `claude login` (OAuth, no API key). `claude --version`
> must be ≥ `MIN_CLAUDE_VERSION`. Default prompt language is Vietnamese.

## Architecture

```
Slack (Socket Mode, app_mention)
  └── slack_handlers      allowlist channel · fetch thread · post placeholder
        └── worker.JobRunner          async queue, per-thread busy set
              └── dispatcher          resolve worktree · persist active ticket
                    └── run_brain_session
                          ├── ThreadSessionManager   1 ClaudeSDKClient / thread (pooled, idle-TTL evict, resume)
                          ├── ClaudeAgentOptions      system prompt + MCP @tools + sub-agents + hooks + permission cb
                          └── client.query(...)       SDK orchestrates tool_use + Task (no Python ReAct loop)
```

- One `ClaudeSDKClient` per Slack thread, resumed across restarts via `threads.sdk_session_id`.
- Sub-agents (po / ba / dev / review) are `AgentDefinition` entries reached through the native `Task` tool.
- Integrations are in-process MCP `@tool`s. Permission confirm (merge/approve PR) is a `can_use_tool` callback → Slack ✅/❌ buttons.
- State (run logs, threads, messages, service registry) lives in SQLite.

Deeper reference: **[CLAUDE.md](CLAUDE.md)** (current architecture, request lifecycle,
permission flow, hooks) and **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (how the SDK
client is wrapped). Migration history: **[MIGRATION_PLAN.md](MIGRATION_PLAN.md)** (the
legacy `claude -p` subprocess path was removed 2026-05-30).

## Prerequisites

1. **Claude CLI** installed and authenticated: `claude login`. Verify with `claude --version`.
2. **Slack app** with Socket Mode enabled. Bot scopes: `app_mentions:read`, `chat:write`.
   Subscribe to the `app_mention` event (DMs are intentionally ignored). Generate an
   **app-level token** with `connections:write`.
3. Python 3.11+.

## Install

```bash
cd /Users/tyron/Projects/agentic
make install           # .venv + editable install + copy .env.example -> .env
# then edit .env (see .env.example for the full list)
```

> **Lấy key & chạy từ A-Z:** [docs/SETUP.md](docs/SETUP.md) — cách lấy token Slack /
> GitHub / Jira / Notion / Grafana, điền `.env`, và toàn bộ lệnh Makefile.

Required env: `SLACK_BOT_TOKEN` (xoxb-), `SLACK_APP_TOKEN` (xapp-, Socket Mode).
Optional: `GITHUB_TOKEN` + `GITHUB_DEFAULT_REPO`, `JIRA_*`, `SLACK_ALLOWED_CHANNELS`,
`WORKER_CONCURRENCY`, `CLAUDE_BIN`, `WORKSPACE_DIR`/`WORKTREE_DIR`, `AGENTIC_SERVICES_JSON`,
`SDK_SESSION_IDLE_TTL_S`, `SDK_MAX_CONCURRENT_SESSIONS`, `BRAIN_MODEL`, `MIN_CLAUDE_VERSION`.

## Run / debug / restart

Use the [Makefile](Makefile) — do not invent equivalents.

| target | behavior |
| --- | --- |
| `make run` | foreground, Ctrl-C to stop |
| `make debug` | foreground with `LOG_LEVEL=DEBUG` |
| `make start` | background; pid → `.agentic.pid`, logs → `agentic.log` |
| `make stop` / `make restart` / `make status` | manage the background process |
| `make logs` | `tail -f agentic.log` |
| `make test` | run `pytest -q` |
| `make db-show` | last 20 rows of the `runs` table |
| `make db-stats` | cache_read ratio · cost-per-thread · tool fail rate |
| `make db-reset` | wipe `agentic.db` (recreated on next start) |
| `make clean` | drop pyc / pytest / build caches |

Expect `⚡️ Bolt app started (Socket Mode)` on startup, then `@mention` the bot in an
allowed channel.

> Run a single bot instance only — multiple Socket Mode instances split events
> non-deterministically. `make stop` before `make debug`.

## Layout

```
src/agentic/
├── main.py            # Bolt entrypoint + startup checks + idle session sweep
├── slack_handlers.py  # app_mention handler + permission button handlers
├── worker.py          # JobRunner async queue
├── dispatcher.py      # thin per-turn orchestration → run_brain_session
├── policy.py          # per-channel tier policy (prod vs revamp)
├── sdk/
│   ├── brain_session.py  # run_brain_session + ClaudeAgentOptions factory
│   ├── client_pool.py    # ThreadSessionManager (per-thread client pool)
│   ├── sub_agents.py     # po/ba/dev/review AgentDefinitions
│   ├── mcp_tools.py       # in-process MCP @tools
│   ├── permission.py      # can_use_tool callback + Slack confirm Futures
│   ├── hooks.py           # audit + per-tool runs logging
│   └── session_store.py   # session resume/state persistence
├── agents/base.py     # load_prompt helper
├── prompts/           # markdown system prompts (brain + per-role)
├── integrations/      # github, jira, notion, grafana, git, ... (execute_action + ToolResult)
├── revamp_pipeline.py # da-api revamp pipeline
├── store.py           # SQLite (runs / threads / messages / service_repos)
└── config.py          # pydantic-settings .env loader
```

## Tests

```bash
make test    # pytest -q, hermetic — no live claude / Slack / GitHub calls
```
