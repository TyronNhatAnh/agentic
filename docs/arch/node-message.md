# node-message (Node.js) — legacy order push/SMS socket server ("MessageServer")

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Repo `gogovan-korea/node-message`,
`package.json` name `message-server`, entry `message.js`. Flat, un-namespaced Node
(no `src/`), Node.js legacy era — no release-branch convention confirmed (no `releases/*`
checked yet); read from default branch. **Not in `services.json`/architecture map until
now** — added after a PR review (#56) surfaced it with no local clone or arch coverage.

## Role

A standalone TCP/socket.io server (`net`/`socket.io`, ports `config.server.netPort` /
`webPort`) that **web-api** (the legacy Java monolith) pushes order events into over a
raw socket, using a small binary/JSON protocol (`packetinfo.js` `Protocol.MsgOrderData`,
`MsgSendSMS`, ...). It then fans out to: FCM (driver + b2c push via `fcmservice.js`),
SMS (writes a queue row, see below), and ops email (SMTP via `nodemailer`, rate-limited/
skipped by `SkipInterval`).

**CONFIRMED caller:** `web-api`'s `NetSocketServiceImpl`/`DummyNetSocketServiceImpl`
(`socket.message.server` config) is the only client found — `sendOrderToDriver`,
`MsgOrderData{Released}` etc. are fired from `OrderServiceImpl` (release/accept flows).
No Go service calls this directly (grepped order-service/common-service — no hits).

## Data

MySQL `poolCluster` with 3 named connections (`gogovancode.js` `Code.Connection`):
`master`, `read_replica`, `sms` — config file `<env>_message_config.json` (not in repo,
`library.js` loads `./<prefix>message_config.json` by `process.argv[2]` env prefix).
Reads `OrderRequest`/`OrderFlag` (PascalCase — legacy `gogovan` MySQL schema, same
family as web-api's tables) for the `smsOrderInfo` create/cancel/arranged/completed
paths (`messageservice.js`).

**Shared table found — cross-check this before trusting either doc in isolation:**
`MessageDao.createSms` → `INSERT INTO MSG_QUEUE (MSG_TYPE, DSTADDR, ...)` on the `sms`
connection. `MSG_QUEUE` is the **same table** the Go `notification-service` calls its
"legacy KR SMS/Kakao gateway" fallback (see [notification-service.md](notification-service.md)).
Two independent writers into one legacy queue table — a PR touching either side should
check the other doesn't assume it's the sole producer.

## Review flags

- `processSendSMS` (`message.js`) — the socket-protocol `MsgSendSMS` handler body is
  **empty** (dead code); actual SMS dispatch goes through `messageservice.js`'s
  `smsOrderInfo`/`Dao.createSms` path instead. HYPOTHESIS: the socket SMS protocol path
  is unused/legacy-dead — verify before assuming a change to it has any effect.
- `MessageService.reloadConfiguration` does `eval(...)` on a `Configuration.DataText`
  DB column (`SmsOrder`/`FcmOrder` keys) — code-as-config from the DB, not sandboxed.
- No test suite found in the repo (flat JS, no `test/`/`spec/` dir, no test runner in
  `package.json`).
- No local repo mapping existed until 2026-08-03 (this doc + `services.json` entry) —
  earlier reviews of node-message PRs had no way to check out the PR head or cross-file
  context; treat findings from before that date as diff-only.
