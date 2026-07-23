# report-service (Go) — B2B bulk order import (NOT reporting)

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.129`
(its latest). **Naming trap:** despite the name it does no reporting/analytics — its
only live capability is admin B2B bulk order upload. The report scaffolding
(`OtherService`, `baseQueryRepository`) and its gRPC `OrderService` are empty stubs.

## Owns
Admin B2B **bulk order import**: parse an uploaded `.xlsx`, validate rows against
user/common/order, submit orders in bulk (synchronous request/response). Serves
admin/B2B web (`stag-cw.gogox.co.kr`, `web.business-staging.gogovan.co.kr`).

## Inbound
- HTTP `/api/v1/order-bulk/{validate,submit,template}` (auth) — the whole product.
  Router `internal/api/http/v1/routes.go:26`. Also dev-only `/test-*`, health, swagger.
- gRPC `OrderService.GetOrderByOrderRequestID` — **empty stub** returning an empty
  response (`internal/api/grpc/order_grpc.go`, proto `test.proto`).

## Calls out (all sync gRPC)
- **order** — `CheckEstimateOrder`, `CreateOrderBulkUpload`.
- **common** — `GetVehiclePoolValid`, `GetGoodsTypeValid`, `GetAddress`, `PresignedUrl`.
- **user** — `GetUserIdByUserCode` (Mobis-user check).
- `DaService` HTTP client is wired (`wire_gen.go:33`) but **never invoked** — dead code.

## Async / Data
No Kafka/SQS/cron. Redis caches parsed rows (5-min) between validate and submit.
Opens MySQL connections but **executes no SQL** — the repo layer
(`baseQueryRepository.GetBaseInfo`) is a stub; all business data comes via the gRPC
services above. Excel via `github.com/xuri/excelize/v2`. S3 template URLs obtained
through common's `PresignedUrl` (no direct AWS SDK).

## Core flows
- **Validate** (`POST /order-bulk/validate`): parse Excel "Order" sheet in parallel →
  Mobis check (user) → vehicle-pool/goods check (common) → geocode (common) + fare/
  validation (order `CheckEstimateOrder`) → cache rows in Redis under a UUID → return
  rows + errors + key.
- **Submit** (`POST /order-bulk/submit`): load cached rows by key → order
  `CreateOrderBulkUpload` → return per-row results.
- **Template** (`GET /order-bulk/template`): common `PresignedUrl` per configured S3 key.
