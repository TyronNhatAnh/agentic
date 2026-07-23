# dhlex-service (Java) — DHL parcel-scan → GoGoX order bridge

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.68`
(**STALE / dormant** — last commit 2023-11, "Switch to DHL Mock API"). Spring Boot
**3.1**, Java 17, reactive **WebFlux + R2DBC**, org `gogovan`.

## Owns
Despite the "DHL Express" name it creates **no** shipments/labels/rates. It is a **DHL
shipment-tracking QUERY → GoGoX order-creation bridge** for KR last-mile: a branch user
scans DHL parcel barcodes → the service queries DHL `BPIShipmentQuery` for parcel detail
→ resolves a KR address via Korea Post (epost) → stores each parcel as a `scanned_item`
→ batch-submits selected items to **order-service** to create B2B delivery orders.

## Inbound (reactive REST, base `/api/v1`)
- `/scanned-items` — `GET /{id}`, `POST /info` (scan→DHL query→persist), `DELETE`,
  `PATCH`, `POST /list`.
- `/job` — `POST /submit` (submit a group to order-service), `GET /{status}`,
  `GET /status/{jobId}`.
- `/groups`, health, actuator on mgmt port 6001.
- Auth: caller JWT decoded locally with **no signature verification**
  (`util/JWTUtil.java`) — **review flag** — token then forwarded to order-service.

## Calls out (reactive `WebClient`, `integration/`)
- **order-service** — `POST api.gogox.co.kr/order/api/v1/b2b-orders-dhl`
  (`PricingIntegrationGateway`), forwards caller token, async fire-and-forget in
  partitions of 10. The primary GoGoX data exchange.
- **DHL BPIShipmentQuery** — `POST ${dhl.endpoint}` Basic auth (stag → a mock
  `ggtest.luke.vn/dhlex.php`). Pull model, no tracking webhooks.
- **Korea Post (epost)** — `GET openapi.epost.go.kr` postal-code → road address.
- No web-api / common calls.

## Async / Data
**No Kafka, no scheduler/polling, no SFTP** — request-driven; async is reactor
fire-and-forget only. **Own dedicated PostgreSQL** DB `dhlex`
(`gogox-kr-dhlex-cloudsql`), R2DBC + Flyway — NOT the shared `gogovan` MySQL. Tables:
`scanned_items`, `scanned_items_submitted_job`.

## Core flows
- **Scan/enrich** (`POST /scanned-items/info`): DHL query → map Shipment→`ScannedItem`
  (Shipper→receiver/destination waypoint; hardcoded `vehiclePoolId=4`, `platformCd=8`,
  `pay="Credit"`) → epost address → dedupe → persist NEW.
- **Submit** (`POST /job/submit`): gather NEW items → map to `OrderCommandRequest` →
  async partitioned POST to order-service `b2b-orders-dhl` → mark COMPLETED/FAILED.

## Review flags
JWT accepted without signature verification; hardcoded DHL Basic auth + epost ServiceKey
+ DB creds in `application-local.yml`. Treat as dormant — confirm it's still deployed
before investing review effort.
