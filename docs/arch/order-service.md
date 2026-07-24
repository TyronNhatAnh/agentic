# order-service (Go) — order system of record

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`.
Gin + gRPC, shared `gogovan` MySQL. Module `github.com/gogovan/ggx-kr-order-service`.

## Owns
Order lifecycle (submit/update/cancel/reorder/tip), admin dispatch & assignment,
home-moving orders, coupons, e-tax, statement-of-use reports, B2B DHL/Mobis integration.
System of record for `orderrequest` & friends.

**Owns the core estimate/quote API** — `POST /api/v1/estimate` (auth) + `/guest/estimate`
(`routes.go:56,129` → `orderHandler.Estimate*`), implemented in
`internal/application/order/queries/estimate/`. Estimate logic lives **here**, not in
common (which only supplies pricing reference data) or web-api (its own separate `fares`
engine). A PR changing estimate/quoting behavior is an order-service change.

## Inbound
- HTTP (`:5000`): `/api/v1/{orders,guest,admin,report,etax,home-moving,coupons}`.
  Router `internal/api/http/v1/routes.go`.
- gRPC (`:5001`, `internal/api/grpc/order_grpc.go`): `OrderGrpcService`,
  `OrderService`, `OrderPoolService`, `RegionService`. Surfaces siblings call:
  `GetVwOrderForDa`, `GetDriverPrice`, `GetOrderPoolIDsByOrderRequestID`,
  `GetPriceIdsByOrganizationId`, `VerifyBizRegistrationNumber`, `AddOrgPricing`,
  `GetActiveOrderCountByUserId`, `CheckEstimateOrder`, `CreateOrderBulkUpload`.

## Calls out
- **user, driver, common, notification** — sync gRPC (clients dialed in
  `startup/startup.go`, held in `internal/base_service/base_service.go`).
- **legacy business backend** — sync HTTP as `DaService` → `business-staging.gogovan.co.kr`
  (config `daservice.webApi`, `config/stag/config.yaml:96-97`). Order notify/webhook/
  coupon/etax. See the "DA trap" in the index — this is NOT the Rails da-api.
- **Barobill** (e-tax), **Kakao Map/Mobility** (routing/geocode), S3, Slack.

## Async
Kafka **producer** — topic `gogovan.consumer-web.order.submit-order`
(`config/stag/config.yaml:61`), injected into `SubmitB2BHandler`. This is the
dispatch handoff driver-service consumes. No consumer. Redis = cache only.
HYPOTHESIS: the explicit `.Produce` call site wasn't confirmed by grep; the legacy
business backend may also be a producer of this topic (see web-api detail).

## Data (`gogovan` MySQL)
`orderrequest`, `orderamount`, `orderhist`, `orderflag`, `orderetax`, `orderowner`,
`orderpool`, `waypoint`, `zoneprice`, `coupon`, `appliedcoupon`, `extraprice`,
`specialprice`, `goodsinfo`, `region`. Read/write/replica DB split. See
[DB_TABLES.md](../DB_TABLES.md) for naming.

## Core flows
- **Submit → dispatch:** submit handler persists `orderrequest`/`orderamount`/
  `waypoint` → notifies business backend (DaService webhook) → emits Kafka
  submit-order → driver-service consumes → filters drivers → push.
- **Estimate/quote:** Kakao Map distance/time + pricing entities; org price IDs via
  `common.GetPriceIdsByOrganizationId` (order and common call each other here —
  watch for cycles when touching pricing).
- **Admin dispatch:** `/api/v1/order-control/*` → `search_order` + `assign_multiple`
  → updates `orderrequest`/`orderpool` → pushes to DA + driver/notification.
