# agentic — Slack multi-agent bot powered by Claude CLI

A Slack bot whose "brain" routes user requests to specialized sub-agents (BA, PO, Dev, Review).
Each agent is a subprocess invocation of the local `claude` CLI with a role-specific system prompt.

## Architecture

```
Slack (Socket Mode)
   └── slack_bolt AsyncApp
         └── dispatcher
               ├── brain  (claude -p, returns JSON plan)
               ├── agents/{ba,po,dev,review}  (claude -p with role prompts)
               └── integrations/github  (REST API)
```

State (run logs, threads) is stored in SQLite.

## Prerequisites

1. **Claude CLI** installed and authenticated: `claude login`. Verify with `claude -p "hello"`.
2. **Slack app** with Socket Mode enabled. Bot scopes: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`. Subscribe to events: `app_mention`, `message.im`. Generate an **app-level token** with `connections:write` scope.
3. Python 3.11+.

## Install

```bash
cd /Users/tyron/Projects/agentic
make install           # creates .venv, installs editable + dev extras, copies .env.example -> .env
# then edit .env: SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-), GITHUB_TOKEN (optional)
```

Manual equivalent:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## Run / debug / restart

Use the [Makefile](Makefile) for day-to-day:

| target | behavior |
| --- | --- |
| `make run` | foreground, Ctrl-C to stop |
| `make debug` | foreground with `LOG_LEVEL=DEBUG` |
| `make start` | background; pid → `.agentic.pid`, stdout/stderr → `agentic.log` |
| `make stop` | stop the background process |
| `make restart` | `stop` + `start` |
| `make status` | check whether the background process is running |
| `make logs` | `tail -f agentic.log` |
| `make test` | run pytest |
| `make db-show` | last 20 rows of the `runs` table |
| `make db-reset` | delete `agentic.db` (recreated on next start) |
| `make clean` | drop pyc / pytest / build caches |

Expect `⚡️ Bolt app started (Socket Mode)` on startup. Then in Slack, DM the bot or `@mention` it in a channel.

## Smoke tests

- `chào` → brain replies directly, no agent run.
- `viết user story cho tính năng login bằng Google` → BA agent emits Given/When/Then.
- `review diff sau:\n<paste diff>` → Review agent.
- `tạo github issue cho story trên` (after a BA run) → brain emits `github.create_issue`; requires `GITHUB_TOKEN` + `GITHUB_DEFAULT_REPO`.

## Inspect runs

```bash
make db-show
# equivalent:
sqlite3 agentic.db "select id, agent, status, duration_ms, substr(input,1,60) from runs order by id desc limit 20;"
```

## Tests

```bash
make test    # or: pytest
```

## Layout

```
src/agentic/
├── main.py            # Bolt entrypoint
├── slack_handlers.py  # app_mention / message.im handlers
├── dispatcher.py      # orchestrates brain → agents → integrations
├── brain.py           # JSON-output orchestrator (claude -p)
├── agents/            # ba, po, dev, review (each = claude -p + system prompt)
├── prompts/           # markdown system prompts per role
├── integrations/      # github (MVP); jira/linear post-MVP
├── store.py           # SQLite logging
└── config.py          # pydantic-settings .env loader
```

## Post-MVP

- Jira / Linear integrations.
- Streaming Slack updates (chunked stdout from `claude -p --output-format stream-json`).
- Per-thread Claude session (`claude --resume <conv-id>`) for long-running multi-turn contexts.
- Slash commands for explicit role routing (`/ba`, `/dev`, ...).
