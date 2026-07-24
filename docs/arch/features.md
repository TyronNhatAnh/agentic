# GoGoX KR — feature map (which services a change touches)

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). **Start here when reviewing
a PR**: find the feature the change belongs to, then read the `arch/<service>.md` for
*every* service in its row — not just the repo the PR sits in. Most real bugs are on the
edges between these services; the "Watch" column is where they hide.

Services span the Java monolith (`web-api`) ↔ Go services mid-migration, so many features
have a legacy path and a Go path live at the same time — check which one the diff is on.

---

## Order & dispatch

**Order submit** — `order`, `web-api`, `da-api`, (`web-b2b`/`api-layer` as callers)
- Entry: order `internal/application/order/commands/submit_b2b/submit_b2b_handler.go`; web-api `OrderController` `/api/order/submit/*`; BFFs `b2_c*.rb`.
- Data: `orderrequest`/`orderamount`/`waypoint`; Kafka `gogovan.consumer-web.order.submit-order`.
- Watch: **two order writers** (Go order-service + web-api) — confirm which path the PR is on. web-api is the primary owner of the tables. A submit that doesn't emit the Kafka event breaks dispatch silently.

**Dispatch / driver assignment** — `order` → (Kafka) → `driver`, `user`, `notification`
- Entry: driver `internal/api/kafka/submit_order_consumer.go` → `GetDriverByFilter` → `NotificationSubmitOrder` (`driver_service.go`).
- Data: topic `...submit-order` (producer=order, consumer=driver — strings must match exactly); `pushfilter`, `vehiclepool`, org-17 special case.
- Watch: driver's push **producer is commented out** in this build. Changing the topic name on one side only silently drops all dispatch. Push keys come from user-service gRPC.

**Admin dispatch / assign** — `order`, `driver`, `notification`, `web-admin`
- Entry: order `order_control_handler.go` (`search_order` + `assign_multiple`); web-admin `AjaxController`.

**Legacy order state transition (accept/release/complete)** — `web-api`, `da-api`
- Entry: web-api `OrderController` `/api/order/{accept,release}/driver` (:970/:996), `/webhook` (:1049); da-api `OrderService#assign_order`/`complete_order`.
- Watch: this is **web-api's job, not the Go order-service**. apikey = `Crypto.getToken(driverId)` (salt `9090van`). da-api takes a Redis lock before accept.

## Pricing & money

**Pricing / estimate** — `order` (**owns core estimate API**), `common` (pricing reference data), `web-api` (fares engine)
- Entry: order `internal/application/order/queries/estimate/estimate_handler.go` (routes `POST /estimate`, `/guest/estimate`); common `internal/application/implement/vehicle_service.go` (`GetPriceIdsByOrganizationId` only); web-api `PricingCalculationEngineV1`.
- Data: `zoneprice`, `extraprice`, `specialprice`, `organizationpricing`; web-api → `staging.api.gogox.co.kr/fares/orders/{id}`.
- Watch: **the core estimate API is order-service** — common only supplies price IDs/reference data, web-api `fares` is a downstream call. Don't attribute estimate to common/web-api. `order↔common` call each other (`GetPriceIdsByOrganizationId`) — cycle risk; Kakao Map supplies distance/time.

**Payment / charge / reconciliation** — `payment`, `order`, `user`
- Entry: payment `PaymentServiceImpl` (`/payment/make-payment`), `ReconciliationScheduler`; gateways `OrderIntegrationGateway`/`UserIntegrationGateway`.
- Data: **Postgres** `payment_request`/`card_payment` (NOT `gogovan` MySQL); Toss Payments.
- Watch: payment→order `POST /orders/status` after charge; payment→user `/toss/add-card`. Different DB — DB_TABLES.md naming does not apply.

**Bank-account (payout) verification** — `driver`, `payment`, `da-api`, `web-admin`
- Entry: driver `bank_verify_handler.go` → payment `/api/v1/bank-account/verify-holder-real-name`; web-admin `AjaxController:2499+`.
- Watch: driver/da-api/web-admin all proxy to payment; caller JWT forwarded. Real-name check goes to Toss v2.

## Identity, address, messaging

**Identity / auth / KCB** — `user`, `da-api`, `web-api`
- Entry: user `internal/api/http/v1/kcb_handler.go`, social login `auth_handler.go`; user→`business` DA web API for KCB handshake/`login-da`.
- Data: `user`, `authentication`, `kcb_*`; ok-name API. Admin RBAC is separate (see below).
- Watch: KCB verification is delegated to user-service from da-api/order paths; DI linkage matches users by DI→phone→name.

**Address / geocoding** — `common` → `web-api` (business)
- Entry: common `GetAddress` → business `/api/service/address/search` + `/addressdetails/search`.
- Watch: **everyone geocodes through common-service, not the legacy API directly.** lat/lon scaled ×1e5. Certain address formats (inline unit number) return 0 rows.

**Notification / OTP / push** — `notification`, `user`, `common`
- Entry: notification `send_otp_sms_handler.go`, `notification_service.go`; called by user/order via gRPC.
- Data: `CustomerNotify`, `MSG_QUEUE`, OTP in Redis; providers FCM/CJ MPlace/MessageBird/Twilio/SendGrid.
- Watch: notification's **Kafka consumer is dead** — delivery is gRPC/HTTP only. Hardcoded SendGrid key in stag config (secret leak).

**Driver location** — `driver`, `da-api`
- Entry: driver `POST /guest/driver-location` → Firestore (AES-128); da-api `firestore_service.rb`, GGT → Kinesis.
- Watch: location is in **Firestore**, not MySQL; encrypted at rest.

## Schema & data

**DB schema migration (`gogovan` MySQL)** — `common` **ONLY**
- Entry: common `migrations/*.up.sql` / `*.down.sql`. Other Go services carry migration *tooling* in `vendor/…/command/migration*` but **no migration files** — they do not run migrations.
- Watch: a PR that adds/alters a `gogovan` table must land in **common-service**. A migration file in order/user/driver/notification/report is misplaced — flag it. Shared views (`vworderforda`, …) are defined by common's migrations too.

## B2B, admin, batch

**B2B corporate order** — `web-b2b`, `web-api`, `user`, `order`
- Entry: web-b2b `OrderController` → web-api `/order/*/b2b`; org/pricing via user `AddOrgPricing` + `organizationpricing`.
- Watch: web-b2b hits `gogovan` DB directly for context but delegates lifecycle to web-api; no Go/Kong calls.

**Bulk order import** — `report`, `order`, `common`, `user`
- Entry: report `internal/application/.../order_bulk_service.go` (`/order-bulk/validate|submit`).
- Watch: **`report-service` does no reporting** — it's this. It runs no SQL; all data via gRPC. Rows cached in Redis between validate and submit.

**Admin panel / RBAC** — `web-admin`, `web-systemAdmin`, (`user` for operators, NOT admin RBAC)
- Entry: web-admin controllers + `PermissionRegistry`; web-systemAdmin `HandlerInterceptor` + `adminrolemenupermissions`.
- Data: RBAC data in `gogovan` (`adminuser`, `adminrolemenupermissions`, `menus`).
- Watch: **admin RBAC is enforced in-process against `gogovan` tables, NOT via user-service.** Editing UI is web-admin (embedded as an iframe in web-systemAdmin). Legacy Java web apps here have real security debt (SQLi `${}` in web-systemAdmin, EOL Spring, `httpOnly=false`).

**DHL parcel → order (dormant)** — `dhlex`, `order`
- Entry: dhlex `JobServiceImpl.submit` → order `/order/api/v1/b2b-orders-dhl`.
- Watch: dormant (2023). Own Postgres. JWT accepted without signature verification.

**Admin AI chatbot** — `ai-admin-assistant`, all Go services (read), `web-admin` (events)
- Entry: ai-admin `/api/v1/chat`, `/internal/orders/{submitted,cancelled}` (HMAC from web-admin).
- Watch: read-only over the Go APIs (forwards AdminUser JWT); Gemini LLM. Separate from the `agentic` Slack bot.

---

*Coverage: features derived from the 15 mapped services (release DAPro-2.130). If a PR
doesn't fit a row here, fall back to the call graph in the index + the repo's own
`arch/<service>.md`, and add a feature row when the gap is real.*
