# GoGoX Korea — backend system map (index)

How the GoGoX KR backend fits together, so reviewing a PR or answering an
architecture question starts from the real topology instead of guessing from one
repo. A **map, not the territory**: the code on the latest release branch is the
source of truth. Inferred claims are marked HYPOTHESIS, unconfirmed ones UNKNOWN.

## How to use this doc (read this — don't load everything)

This index is small and holds only the **cross-cutting** knowledge: topology, the
call graph, the naming traps, and a service table. **Per-service detail lives in one
file each under `docs/arch/<service>.md`** — load only the ones you need.

- **Reviewing a PR:** read this index, then [`docs/arch/features.md`](arch/features.md)
  to find the feature the change belongs to and the **full set of services it touches**
  (features cross service boundaries — the repo the PR sits in is rarely the whole
  story), then read `docs/arch/<service>.md` for each service in that feature's row.
  Don't read all of them.
- **Architecture question:** read this index; open a detail file only if the answer
  needs it.

The detail files sit next to this file (same `docs/` tree). This index was itself
opened at a known absolute path, so resolve siblings there (e.g.
`.../docs/arch/order-service.md`).

## The shape in one paragraph

The estate is **mid-migration from a Java monolith to Go microservices**, and that
theme explains almost everything. The **Go** services (order, user, driver, common,
notification, report — Gin + gRPC) talk to each other over **synchronous gRPC**; the
**only live cross-service async event** is the order→driver dispatch handoff over Kafka.
Payment is Java/Spring on its **own PostgreSQL**; everything else shares one **legacy
`gogovan` MySQL**. The **legacy Java monolith** — `web-api` at `business.gogovan.co.kr`
— is still the **primary owner of the order tables** and owns order state transitions;
the Go services call it under the misleading name "DaService". Several **front doors**
sit on top: `da-api` (Rails, driver app) and `api-layer` (Rails, customer app) are BFFs
that fan out to both web-api and the Go services (each with a legacy-path/Go-path split
of its own); `web-admin`/`web-b2b`/`web-systemAdmin` are Java JSP apps that hit the
`gogovan` DB directly **and** proxy order ops to web-api. Traffic enters through a **Kong
gateway** (`{env}-api.gogox.co.kr/{service}/...`). Two edge services: `dhlex` (DHL
parcel→order bridge, dormant) and `ai-admin-assistant` (Python/Gemini admin chatbot over
the Go APIs).

---

## Service table

| Service | Stack | Role | Detail |
|---|---|---|---|
| order-service | Go | Order system of record: lifecycle, pricing, dispatch, e-tax | [arch/order-service.md](arch/order-service.md) |
| user-service | Go | Identity, auth, org/B2B, RBAC, KCB real-name | [arch/user-service.md](arch/user-service.md) |
| driver-service | Go | Drivers, vehicles, driver-side dispatch, location | [arch/driver-service.md](arch/driver-service.md) |
| common-service | Go | Reference data: common codes, vehicle/pricing, address, audit log | [arch/common-service.md](arch/common-service.md) |
| notification-service | Go | Outbound messaging (push/SMS/KakaoTalk/OTP) — a sink | [arch/notification-service.md](arch/notification-service.md) |
| report-service | Go | **B2B bulk order import** (NOT reporting) | [arch/report-service.md](arch/report-service.md) |
| payment-service | Java/Spring | Card payments (Toss) + bank verify — **own PostgreSQL** | [arch/payment-service.md](arch/payment-service.md) |
| da-api | Ruby/Rails | Driver-app backend — a *client* of the mesh | [arch/da-api.md](arch/da-api.md) |
| web-api | Java (Spring MVC 4.2) | **The legacy "business" backend / DaService** — primary owner of order tables | [arch/web-api.md](arch/web-api.md) |
| web-admin | Java (Spring MVC 3.2) | Admin panel web app — own DB + proxies to web-api / Go services | [arch/web-admin.md](arch/web-admin.md) |
| web-b2b | Java (Spring MVC 3.2) | B2B corporate portal (JSP) — own DB + delegates orders to web-api | [arch/web-b2b.md](arch/web-b2b.md) |
| web-systemAdmin | Java (Spring MVC 3.2) | Super-admin/system console — owns admin RBAC data in `gogovan` | [arch/web-systemAdmin.md](arch/web-systemAdmin.md) |
| api-layer | Ruby/Rails | Customer-app **BFF** over web-api + Go services | [arch/api-layer.md](arch/api-layer.md) |
| dhlex-service | Java (Spring Boot 3.1) | DHL parcel-scan → GoGoX order bridge (**dormant**) | [arch/dhlex-service.md](arch/dhlex-service.md) |
| ai-admin-assistant | Python (FastAPI) | KR admin **chatbot** (Gemini) over the Go services | [arch/ai-admin-assistant.md](arch/ai-admin-assistant.md) |

Not yet mapped (see §5): a separate GoGoX **pricing/fares** service; dead repos
(web-b2c, web-driver, gogox-service); da-api-v2. web-library is the shared Java client lib.

---

## Topology & conventions

- **Gateway (Kong):** `https://stag-api.gogox.co.kr/{service}/api/v1/...` (staging),
  `https://api.gogox.co.kr/{service}/...` (prod). One AdminUser JWT is validated by
  the gateway and authorizes every `/{service}` route. Fronted: order, user, driver,
  common, payment (and `/da-api` → da-api).
- **Internal wiring:** Go services dial each other **directly** (not via Kong) — gRPC
  at `ggx-kr-{svc}-{env}:5001`, HTTP at `:5000`. da-api (outside the mesh) reaches the
  Go services through the public Kong gateway.
- **Transports:** Go↔Go = gRPC. payment↔(order,user) = sync REST (RestTemplate, no
  Feign). da-api→everything = Faraday HTTP. Async = Kafka (one live edge only).
- **Datastores:** order/user/driver/common/notification/da-api → shared legacy
  **`gogovan` MySQL** (see [DB_TABLES.md](DB_TABLES.md)). payment → its own
  **PostgreSQL** (`payment` schema). driver live location → **Firestore** (AES-128).
  report opens MySQL but runs no SQL. Redis = cache / lock, not a bus.
- **Deploy/observability:** Argo, pods `argo-ggx-kr-{service}`, namespaces
  `kr-stag`/`kr-prod`. Grafana Loki; every request carries a `req_id`
  (`X-Request-ID`) for cross-service tracing.
- **Secrets:** HashiCorp Vault (`vault-v2.gogo.tech`). Slack webhooks for panic alerts.

---

## Naming traps (read before reviewing order/driver/report work)

**1. "DA" is overloaded — three different things:**
- **`ggx-kr-da-api`** (Ruby/Rails) — the **Driver App backend**, a *client* of the
  mesh. Nothing in the mesh calls into it; the driver app reaches it via Kong `/da-api`.
- **`business.gogovan.co.kr`** — a **separate legacy Java "business" backend** that
  owns order state transitions (accept/release/webhook), etax, coupon, address geocode.
- **`DaService` / `daservice:` config in the Go services** — despite the name, points
  at **the legacy Java backend, not the Rails da-api**. So "order-service notifies DA"
  = it calls `business.gogovan.co.kr`.

Consequence: order state has **two writers** — the Go order-service and the legacy
business backend (`web-api`, reached by both order-service and da-api). Evidence
(verified) says **web-api is the primary owner/writer of the order tables** (MyBatis
INSERT/UPDATE across `orderrequest`/`order`/`orderpool`/`orderamount`; it serves
`/api/order/accept|release|webhook`), with the Go order-service coexisting — a
migration-in-progress split, not a clean handoff. Treat web-api as authoritative for
driver-facing state transitions unless a specific flow proves otherwise. Note the Go
order-service **is** the Kafka `submit-order` producer — web-api's Kafka is disabled
(`enable.kafka=false`), so it is NOT that producer.

**2. `report-service` does not do reporting** — its only live capability is admin B2B
bulk order import (Excel → validate → submit via order-service). Reporting/gRPC
scaffolding is a stub; it runs no SQL. Don't review it as analytics/export.

**Single-owner facts (easy to get wrong in review):**
- **`gogovan` schema migrations → `common-service` ONLY.** It's the one service with real
  `migrations/*.sql`; the others carry migration tooling in `vendor/` but no migration
  files. A migration added in order/user/driver/etc. is misplaced.
- **Core estimate/quote API → `order-service`** (`POST /estimate`, `/guest/estimate`).
  `common` only supplies pricing reference data; `web-api` has its own `fares` engine.
  Don't attribute estimate elsewhere.
- **Order tables → primary owner `web-api`** (Go order-service coexists; mid-migration).
- **payment → own PostgreSQL** (not `gogovan` MySQL). **Admin RBAC → `gogovan` tables**,
  enforced in-process, not via user-service.

---

## Cross-service call graph

```
                         Kafka: gogovan.consumer-web.order.submit-order  (the one live async edge)
   order-service  ───────────────────────────────────────────────▶  driver-service
        │  ▲                                                            │  (consume → filter drivers → push)
   gRPC │  │ gRPC (biz-reg, active-order, org-pricing)                 │ gRPC (order/pool/price)
        ▼  │                                                           ▼
   user-service ──gRPC(config,otp,commoncode,auditlog)──▶ common-service ──gRPC(GetPriceIdsByOrg)──▶ order-service
        │                                                     ▲                                       ▲
        │ gRPC (OTP SMS / KakaoTalk / push)                   │ gRPC (vehicle info)                   │
        ▼                                             driver-service ─────────────────────────────────┘
   notification-service (sink)                                │ REST (bank verify)
        ▲ gRPC (order & user call it)                         ▼
        └───────────────────────────────────────────  payment-service ──REST──▶ order (status) · user (add-card)
                                                              │ REST                    ▲
                                                              ▼                         │ gRPC (estimate + bulk create)
                                                         Toss Payments          report-service (admin bulk upload)

   Legacy Java "business" backend (business.gogovan.co.kr)
        ▲ HTTP "DaService" (order, user, common)   ▲ HTTP order accept/release/webhook
        └──────── Go services                      └──────── da-api (Rails) ──Kong──▶ user/payment/driver/common
```

Read it as: **sync gRPC** is the backbone; **one live Kafka edge** (order→driver)
drives dispatch — every other Kafka topic (notification's OTP/push, user's, common's)
is configured but its consumer is **dead/unstarted**; **payment** reaches in over REST;
**report** fans into order/common/user; **notification** is a pure sink; **da-api** and
the **legacy Java backend** sit at the edges over HTTP.

---

## 5. Not-yet-mapped (leads only — verify before relying)

Fifteen services have detail files (6 Go + payment + da-api + web-api + web-admin +
web-b2b + web-systemAdmin + api-layer + dhlex + ai-admin). Every live service in
`services.json` is now mapped. Still outstanding:

- **GoGoX pricing/fares service** — `web-api` calls `staging.api.gogox.co.kr/fares/orders/{id}`
  for fare computation. Not in `services.json`, no local clone; a real dependency worth
  mapping if you find the repo.
- **web-library** — shared Java client lib (branch `snapshot/2.0`) providing `Crypto`
  (the `9090van` apikey hash), `AuditLogGateway`, `Utility.download`. Not a runtime
  service; extract if you need the exact apikey algorithm.
- **Dead repos (skip):** `web-b2c` (release stuck at DAPro-2.110), `web-driver`
  (DAPro-2.49), `gogox-service` (no release branch, last commit 2022). Confirmed
  abandoned — do not treat as current architecture.
- **da-api-v2** (`TyronNA/ggx-kr-da-api-v2`) — a da-api rewrite? no local clone.

When you need one, extract it the same way (fresh release worktree → routes/clients/
models) and add a `docs/arch/<service>.md` + a row in the service table.

---

*Sources: latest release branch of each of the 15 extracted repos (mostly
`DAPro-2.130`; dhlex `DAPro-2.68`, dormant) + the ops skills (gateway, Loki).
Regenerate when the architecture shifts — a stale map is worse than none.*
