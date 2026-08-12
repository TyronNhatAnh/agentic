# What this bot can and cannot reach

Written for the brain. The point is the **boundaries** — which system holds which
data, and what happens when you ask the wrong one. Most of the tool failures this
file exists to prevent were not bad calls in themselves; they were calls to a
system that never held the thing being asked for, answered with an error that
didn't say so.

## Logs — two estates, no overlap

| What | Tool | Where it lives |
|---|---|---|
| Go services (`ggx-kr-*`), chatbot | `grafana_search_logs` | Loki, via Grafana proxy |
| Java Tomcat: `web-admin` `web-api` `web-b2b` `web-b2c` `web-driver`, `catalina` | `java_logs` | Files on EC2, via the Pi bastion |
| Apache access log, api-layer (`apl`), node-message (`msg`) | `java_logs` | same |

**The Java apps are not in Loki and never will be.** Asking `grafana_search_logs`
for `web-api` returns a `CONFIG` error about a missing `loki_selector`; that error
is correct, and the fix is not to add a selector — a selector pointing at a
nonexistent Loki job would return *empty*, which reads as "no errors found" and is
worse than failing.

Things about `java_logs` worth knowing before your first call:

- **PROD is two Tomcat nodes** (`krprod1`, `krprod2`) and one request touches only
  one. Both are searched by default. A blank result from a single pinned node
  proves nothing.
- **Searching by an id** (order id, phone, request id) → `fast=true`. It greps on
  the EC2 box instead of pulling the file across the VPN: ~10s vs ~80s. `fast`
  takes a bare token only (`A-Za-z0-9_.:-`); a regex or a pattern with spaces has
  to go the slow way.
- **`count=true` is the cheap probe.** Confirming *whether* something happened
  costs one number; read the lines only once you know they exist.
- **Timestamps disagree by log type.** Tomcat and api-layer write UTC; the Apache
  access log and node-message write KST. Pass `kst` (the wall-clock time the user
  reported) and let the wrapper convert.
- **Prod logs are real customer PII** — emails, phones, full API payloads. Quote
  the lines you need and summarize; never dump a block into Slack.

Auth is a Cloudflare Access token on the host, good for ~30 days at a time. When
it lapses the refresh needs a human at a browser, so an `AUTH` failure here means
*tell the user*, not retry.

## Databases

`db_query` is staging, `db_query_prod` is the production read replica. Both run
one read-only statement; writes are rejected client- and server-side, and prod is
`@@read_only=1`. Neither asks for confirmation, and every prod call is audit-logged
server-side.

**Staging and prod are separate schemas, not replicas of each other.** Ad-hoc
`bak_*`/`*_tmp` tables and newer snake_case ones exist in one env and not the
other, so a table confirmed on staging can still 1146 on prod — introspect in the
same env you intend to query. See [DB_TABLES.md](DB_TABLES.md); its column lists
are the common ones, not the full set, so `SHOW CREATE TABLE` before using a
column it doesn't name.

## Code

Two GitHub orgs, and only two: **`gogovan`** holds the Go `ggx-kr-*` services;
**`gogovan-korea`** holds the Java `web-*` apps, `web-library`, `api-layer` and
`node-message`. Any other org slug is invented and returns 422.

`list_services` is the registry — it gives you a service's local clone path,
GitHub slug, and aliases. Use it instead of searching the filesystem: a
`Glob`/`Grep` rooted at `/Users/tyron` walks every checkout on the host and dies
at the 20s limit. Read code from a fresh `git_prepare_read_workspace` path rather
than the raw clone, which can sit on a stale branch.

Services branch off `releases/DAPro-2.<sprint>`, resolved from the Jira active
sprint. The registry is the list of services that exist: if `list_services`
doesn't have it, it is not a service you can work on — say so rather than
inventing a repo name from the company name.

## The architecture map

`{DOCS}/GOGOX_ARCHITECTURE.md` is the index; `{DOCS}/arch/<service>.md` are the
per-service details and `{DOCS}/arch/features.md` maps a feature to the full set
of services it touches. These paths are **absolute** because your cwd is the
thread's service repo, not this one — a relative `docs/...` points at nothing.

## Not reachable

- Company VPN directly (the Pi bastion holds it on the bot's behalf).
- Any Claude Code skill or memory file on the host. Those belong to the
  interactive CLI; this session loads no user/project settings by design. If a
  fact matters to the bot, it belongs in this file, a tool description, or a tool's
  error message — not in memory.
- Writes to production, of any kind.
