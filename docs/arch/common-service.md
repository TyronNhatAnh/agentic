# common-service (Go) — shared reference data

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`.
Gin + gRPC, shared `gogovan` MySQL. A low-level provider called by everyone.

## Owns
Common codes (`commoncode`, the `*CD` lookups), configuration, vehicle catalog +
pricing, home-moving goods catalog + pricing, address search/geocoding, regions,
CMS content/ads, S3 presigned URLs, and the **universal audit log** (`auditlog`,
AES-256-GCM) + action log.

## Inbound
- HTTP: `/api/v1/{vehicles,addresses,admin,guest}` + a service-to-service
  `/api/v1/guest/audit-logs` gated by an HMAC `apikey` (used by the Java web-* apps
  via **web-library** and by Rails da-api).
- gRPC `CommonService`: `GetCommonCode`, `GetConfigurationByKey`,
  `GetVehicleInfoByVehiclePoolIds`, `GetVehicleExtraPrices`, `GetServicesByVehiclePool`,
  `GetAddress`, `CreateAddress`, `PresignedUrl`, `GetRegionById`,
  `GetHomeMovingGoodsAndOptionsByIds`, `SaveActionLog`, `SaveAuditLog`, `ListAuditLogs`.

## Calls out
- **order** (gRPC) — `GetPriceIdsByOrganizationId` during vehicle-price resolution
  (`internal/application/implement/vehicle_service.go`). order↔common form a cycle here.
- **legacy business backend** (HTTP) — `/api/service/address/search` +
  `/api/service/addressdetails/search` for Korean geocoding
  (`internal/infrastructure/external_service/dapro/da_service.go`, `business-staging.gogovan.co.kr`).
- Slack (panic notify). S3, Vault.

## Async
None — Kafka block is dead boilerplate. Config cache is in-memory (5-min TTL), not
Redis pub/sub.

## Data (`gogovan` MySQL)
`commoncode`, `configuration`, `vehicle`, `vehiclepool`, `vehiclepoolmetadata`,
`vehiclemetadata`, `vehicleprice`, `extraprice`, `homemovinggoods*`, `region`,
`content`, `address`, `actionlog`, `auditlog`. Migrations reference order/DA views
(`vworderforda`, `vwordersimpleforadmin`) — HYPOTHESIS: some shared views maintained here.

## Core flows
- **Address + geocode (gRPC `GetAddress`):** → `AddressService` → business backend
  `/api/service/address/search` → first hit → `/addressdetails/search` → return
  address + lat/lon (÷ `LocationRate`) + region. Any service needing an address goes
  through common, not the legacy API directly.
- **Vehicle price resolution:** `vehicle_handler` → `vehicle_service` → order
  `GetPriceIdsByOrganizationId` → query `vehicle`/`vehiclepool`/`vehicleprice`/`extraprice`.
- **Common-code lookup:** gRPC `GetCommonCode(key)` → `commoncode` filtered by
  `ColumnFullName` → codes used by others for `*CD` enum resolution.
- **Audit log:** `SaveAuditLog` → AES-256-GCM encrypt sensitive fields → `auditlog`.
