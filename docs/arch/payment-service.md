# payment-service (Java/Spring) — card payments & bank verification

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`.
Spring Boot, Maven, port 5000, context-path `/api/v1`. Base pkg `com.gogovan.payment`.

## Owns
Customer card payments via **Toss Payments** (billing-key auth, charge, cancel,
status), bank-account real-name verification (driver/user payout accounts), and a
daily reconciliation job (local records vs Toss). Does **not** do driver settlement
payout/invoicing (owner UNKNOWN — not in this repo).

## Datastore — PostgreSQL, NOT gogovan MySQL
Its own **PostgreSQL** `payment` schema, `ddl-auto: none`
(`src/main/resources/application-{local,stag,prod}.yml` → `jdbc:postgresql://...`,
`PostgreSQLDialect`; `pom.xml` org.postgresql). **Do not apply [DB_TABLES.md](../DB_TABLES.md)
naming here.** Entities: `payment_request`, `card_payment`,
`bank_account_verify_{request,response,revoke,history}`.

## Inbound (`@RestController`)
`/api/v1/{payment,bank-account,admin/bank-account,guest/brandpay,guest/diagnostic,
guest/health}`. Controllers in `src/main/java/com/gogovan/payment/controller/`.

## Calls out (all sync RestTemplate — no Feign)
- **order-service** — `POST /api/v1/orders/status` (push result after charge),
  `POST /api/v1/file/reconciliation` (presigned S3 URL). `integration/OrderIntegrationGateway`.
- **user-service** — `POST /api/v1/toss/add-card` (persist card after Toss auth).
  `integration/UserIntegrationGateway`.
- **Toss Payments** — billing auth/approve/cancel, transaction history, bank real-name
  verify (v2), BrandPay. `integration/TossIntegrationGateway`.
- Slack (error + reconciliation webhooks), AWS S3 (reconciliation CSV via order's
  presigned URL). No calls to driver/common/da-api.

## Async
None. Cron `ReconciliationScheduler` (16:00 daily, `job.reconciliation.expression`);
retry scheduler disabled.

## Core flows
- **Card auth:** `POST /payment/authorization-card` → Toss issues billingKey → persist
  card in user-service.
- **Charge:** `POST /payment/make-payment` → Toss `/v1/billing/{billingKey}` → save
  `payment_request` → `OrderIntegrationGateway.updateStatus` to order-service.
- **Reconciliation:** 16:00 → local `payment_request` + Toss history → diff → CSV →
  upload via order-service presigned S3 URL → Slack notify.
- **Bank verify:** `/bank-account/verify-holder-real-name` → Toss v2 → persist
  request/response/revoke. (driver-service calls these endpoints.)
