# ai-admin-assistant (Python) — KR AI admin chatbot

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`, org
`gogovan` (repo `ggx-kr-chatbot-admin-system`, `services.json` name
`chatbot-admin-system`). FastAPI + uvicorn. Reached via Kong under `/ai-admin`.

> Not to be confused with the `agentic` Slack bot this repo (`/Users/tyron/Projects/agentic`)
> implements — this is a **separate KR service** the GoGoX admin team runs.

## Owns
A **read-only internal admin chatbot**: answers FDE/admin questions about orders,
drivers, users/orgs, and indexed code knowledge by combining live GoGoX service-API tool
calls with an offline code index (`indexer/`, FTS5 + graph + semantic). Also runs an
**order-intake track** (Hyundai Mobis dispatch-mail → Postgres review queue → Slack
human-confirm; no auto-submit). LLM = **Google Gemini via Vertex AI** (`google-genai`,
default `gemini-3.5-flash`) — **not Claude**.

## Inbound (routers, `app/main.py`)
- `POST /api/v1/chat`, `GET /api/v1/history[/{id}]` — chat.
- `/api/v1/email-intake/*` — Mobis intake dashboard (read-only).
- `/internal/ingest/run` — manual ingest trigger.
- **`POST /api/v1/internal/orders/{submitted,cancelled}`** — order-event webhooks from
  web-admin, **HMAC-SHA256** over raw body (`X-Order-Event-Signature`); 503 if secret unset.
- `/api/v1/slack/{interactions,commands,events}` — Slack HTTP fallback.

## Calls out (all sync `httpx.Client` via Kong `stag-api.gogox.co.kr`)
- **order / user / driver / common** Go services — `app/services/{order,user,driver,
  common}_service_client.py`. **Forwards the caller's AdminUser JWT** downstream
  (`Authorization: Bearer <token>`), auth delegated to the gateway.
- **Google Gemini / Vertex AI** — `app/llm/gemini_client.py` (SA creds from Vault).
- Gmail (IMAP/SMTP intake), Slack Web API + Socket Mode.

## Async / Data
Order-event webhook handled in a `BackgroundTasks` task (marks the Postgres Mobis review
row idempotently, posts a Korean status reply to the order's Slack thread). In-app ingest
scheduler (Postgres advisory-lock, multi-pod-safe) + idle-leader loop. Orchestrator tool
loop `MAX_TOOL_LOOPS=6`.
- Own state only, **does not touch `gogovan` MySQL** (HTTP-only clients): **Redis**
  (session/conversation), **SQLite** (chat-history fallback), **Postgres** (Mobis
  email-intake: `ingested_emails`, `ingest_cursor`, `turn_metrics`).

## Core flows
- **Admin chat Q&A:** `POST /chat` → `AIOrchestrator` picks a feature (order-lookup,
  driver-tracking, statement-report, user-admin, common-data, knowledge-code,
  mobis-email) → Gemini function-calls the sync service clients (forwarding the JWT)
  and/or the offline index → answer persisted to Redis/SQLite.
- **Order-event notify (Mobis):** web-admin submits/cancels → HMAC `POST /internal/orders/
  {submitted,cancelled}` → mark Postgres review row → Slack thread reply.
