# driver-service (Go) — drivers, vehicles, dispatch, location

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`.
Gin + gRPC, shared `gogovan` MySQL + Firestore + Kafka.

## Owns
Driver / external-driver / vendor records, vehicles & vehicle-pools + their pricing,
driver push-filters, driver location. Distinctive job: **driver-side dispatch** —
given a submitted order, filter eligible drivers (pool/region/distance/fare) and push
new-order notifications. Also bank-holder verification (proxied to payment).

## Inbound
- HTTP: `/api/v1/{driver,admin,vehicles,driver-report,guest}`.
- gRPC: `GetDriverById`, `GetVehiclePoolByIds`, `GetDriverLocationByIds`,
  `GetDriverInfoByIds`, `GetExternalDriverByIds`, `GetVendorInfoByIds`, …

## Calls out
- **order** (gRPC) — `GetVwOrderForDa`, `GetOrderFlagsForNotification`,
  `GetOrderPoolIDsByOrderRequestID`, `GetDriverPrice`.
- **user** (gRPC) — `DevicePushKeyService.GetPushKeysByUserIDs` (push, streaming),
  `GetUserIDsByPoolIDs`, `ExistedOrganizationPool` (org-17 special enterprise check).
- **common** (gRPC) — `GetVehicleInfoByVehiclePoolIds`.
- **payment** (sync REST) — `internal/infrastructure/external_service/payment/payment_client.go`:
  `POST /api/v1/bank-account/verify-holder-real-name` (+admin variants), forwards
  the caller's JWT + OTel headers, 10s timeout.

## Async
Kafka **consumer** (`internal/api/kafka/{consumer,submit_order_consumer}.go`) —
consumes `gogovan.consumer-web.order.submit-order` (`config/stag/config.yaml:50`,
matches order-service's producer topic exactly) → `processSubmitOrder` →
`GetDriverByFilter` → `NotificationSubmitOrder`. This is the receiving half of
dispatch. The push-notification producer is present but **commented out**
(`driver_service.go:219-236`) — HYPOTHESIS: dispatch push incomplete in this build.

## Data
`driver`, `external_driver`, `vendor`, `vehicle`, `vehiclepool`, `vehicleinclude`,
`vehicleregistration`, `extraprice`, `pushfilter`, plus reads of `user`/`organization`.
**Firestore** for live location (AES-128-ECB, MySQL-compatible; key
`database.decryptKey`). Redis = extra-price cache.

## Core flows
- **Dispatch (event-driven):** Kafka submit-order → `GetDriverByFilter` (include-pools
  from DB + order-pool IDs from order gRPC; public vs org via user
  `ExistedOrganizationPool`; candidate drivers by location+pool; apply `pushfilter`) →
  `NotificationSubmitOrder` (push keys from user gRPC, pay info from order gRPC, build
  KR push).
- **Location:** `POST /api/v1/guest/driver-location` → encrypt + write Firestore;
  history reads decrypt (Seoul TZ).
- **Driver fare:** `POST /api/v1/guest/price/:orderId` → order gRPC `GetDriverPrice`.
- **Bank verify:** driver/admin bank-account routes → payment REST (forwards JWT).
