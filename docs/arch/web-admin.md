# web-admin (Java) — admin panel web app

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`,
org `gogovan-korea`. Maven artifact `com.gogovan.admin:admin` (WAR, context `/admin`).

Spring MVC **3.2** + **MyBatis 3.2**, JDK 8, JSP via Apache Tiles 3.0; `/ajax/*`
returns JSON. Served to admin operators' browsers at `web.business-staging.gogovan.co.kr`
/ `business.gogovan.co.kr` (newer screens embedded as iframes; a newer front
`admin.gogox.co.kr` is referenced). **It is the admin app itself, not a BFF/API gateway.**

## Owns / shape
A **hybrid, not a thin proxy**: most functionality is local DB (34 MyBatis DAOs → shared
`gogovan` MySQL, optional read replica via `use.read.replica`), with targeted HTTP calls
out for specific integrations. **RBAC is enforced in-process** (`HandlerInterceptor` +
`PermissionRegistry` against DB `Menu`/`PermissionRegistry`; JWT carries a permission
list) — **not** delegated to user-service.

## Inbound (32 controllers in `controller/`; routes `/<area>`, no `/api` prefix)
`/order`, `/home-moving/order`, `/driver`, `/vehicle`, `/b2buser`, `/b2cuser`,
`/organize`, `/blacklist`, `/pricing`, `/coupons`, `/ratings`, `/region`, `/address`,
`/report`, `/dashboard`, `/backoffice`, `/content`, `/adminuser`, `/system`, `/tool`,
`/ajax/*` (JSON, ~2800-line `AjaxController`), `/login`, `/sociallogin`.

## Calls out (raw `HttpURLConnection` / RestTemplate — no Feign/WebClient, no Kafka)
- **web-api / legacy business backend** — `api.url` (`business-staging.gogovan.co.kr/api`)
  for order submit/cancel/history/price, `/coupon/list`, `/user/withdrawal`,
  `/service/location`, biz-reg verify. `service/impl/InterfaceServiceImpl.java`. Most
  order mutations proxy here.
- **driver-service (Go)** via Kong — `/api/v1/admin/bank-account/verify-holder[,verify-requests,revoke]`
  (`controller/AjaxController.java:2499+`). Sync.
- **common-service (Go)** — audit-log write/read via web-library `AuditLogGateway`
  (HMAC `gogox.audit.service.secret`). `common/AuditLogConfig.java`.
- **ai-admin chatbot (Go)** — async HMAC-signed order-submitted/cancelled callbacks
  `stag-api.gogox.co.kr/ai-admin/api/v1/internal/orders/{submitted,cancelled}` for
  Mobis/preview orders (`service/OrderSubmittedCallbackService.java`, `CompletableFuture`
  + bounded thread pool, 3 retries). This is the AI-admin assistant (KR chatbot), not this
  Slack bot.
- **External:** Atlan/Kakao maps, Call-manager CTI (`218.153.209.226`), S3, social OAuth
  (Google/Naver/Facebook/Daum), socket push servers (`business-staging...:55000/:55011`).

## Async / Data
No Kafka/SQS/scheduler — only the in-process chatbot callback thread pool. Direct
`gogovan` MySQL via 34 DAOs: Order(V2), User, Driver, Organization, Business, Branch,
Vehicle, Pricing, Rating, Region, Address, Waypoint, Coupon, PushFilter, Webhook,
AdminUser, PermissionRegistry, etc.

## Core flows
- **Admin order search/list:** DB-direct via `OrderDao*` (not proxied); newer screens are
  iframes to the legacy admin.
- **Order create/cancel:** proxied to web-api (`api.url`); for Mobis/preview, after web-api
  returns an orderId an async HMAC callback fires to the ai-admin chatbot.
- **Driver bank verify:** `AjaxController` → driver-service Go `/admin/bank-account/*` +
  writes an audit trail via common-service.

## Review flags
Committed static AWS access/secret keys in `stage/application.properties.xml:29-30` (and
`real`); JWT secret `jwt.token.secret=GOGOVAN2022`; legacy Spring 3.2 / JDK 8.
