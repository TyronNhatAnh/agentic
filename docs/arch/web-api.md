# web-api (Java) — the legacy "business" backend (`business.gogovan.co.kr`)

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`,
org `gogovan-korea`. **This is the "DaService" the Go services call** — CONFIRMED
(config `base.url=http://business-staging.gogovan.co.kr/`; the `/api/...` routes below
are cited from source).

Spring MVC **4.2.3** WAR (Tomcat, NOT Spring Boot), JDK 8, **MyBatis 3.2.8** (39 XML
mappers, no JPA). A large monolith `com.gogovan.api.*`. WAR context `api` → routes are
`/api/<group>/...`.

## Owns
Orders (primary owner), pricing, coupons, payments/cards, B2B/B2C accounts, ratings,
e-tax, address/geocode, KCB identity, SMS/ARS, and **driver order state transitions**.
It is the **primary writer/owner of the order tables** (INSERT/UPDATE across
`orderrequest`/`order`/`orderpool`/`orderamount` in `mybatis/Order.xml`,`OrderPool.xml`,
`OrderBind.xml`) — CONFIRMED. This is the counterweight to the Go order-service; see the
order-state-ownership note in the index trap section.

## Inbound (controllers in `src/main/java/com/gogovan/api/controller/`)
Groups: `/order`, `/service`, `/coupon`, `/b2b`, `/b2c`, `/user`, `/payment`,
`/ratings`, `/image`, `/home-moving/order`, `/interface` (SMS/ARS), `/migration`,
`/system`, `/sociallogin`, `/health`. The routes the Go services & da-api hit (all
CONFIRMED):
- `/api/order/accept/driver` (`OrderController.java:970`), `/release/driver` (:996),
  `/webhook` (:1049), `/customer/notification` (:1256)
- `/api/coupon/list` (`CouponController.java:39`)
- `/api/service/{address/search,etax/issue_tax_invoice,driver/verify_biz_registration_number}`
  (`ServiceController.java:174,515,437`)

**apikey:** driver routes → `BaseController.validateApikey` = `Crypto.getToken(driverId)`
(salt `ggv.aes.salt=9090van` CONFIRMED; exact hash lives in external web-library, so
`SHA256("9090van"+id)` is HYPOTHESIS). B2B/order routes → DB per-branch apikey (not a hash).

## Calls out
- **GoGoX pricing/fares service** — `POST staging.api.gogox.co.kr/fares/orders/{orderId}`
  (`PricingCalculationEngineV1.java:802`, config `gogox.service.url`). The only clear call
  into the Go/GoGoX platform. **NB: a separate "fares" service not in `services.json`** —
  a lead worth mapping.
- **External:** Iamport + Barobill (payment PG / e-tax), Kakao/Atlan/Juso/epost (maps/
  address), KCB (identity), odcloud (biz-reg), MessageBird + ARS (`218.153.209.226`),
  Firebase/FCM, Slack, S3, SF-Express (SFTP), DHL (SFTP), prediction ML
  (`gogoml.kr.stag.data.gogo.tech`).
- **da-api:** none outbound — da-api calls *into* this service.

## Async
Kafka **producer only, and DISABLED** — `enable.kafka=false` in every env; the only
topic referenced is `kr-zero-rate-prediction`. **It does NOT produce
`gogovan.consumer-web.order.submit-order`** (REFUTED by grep) — that producer is the Go
order-service. Redis pub/sub channel `pubsub:queue`. **Quartz 2.3.2** scheduled jobs
(`com.gogovan.quartz`, backed by the `gogovan` DB; ShedLock). Order updates fan out to
B2B partners via persisted **webhook** rows (`/api/order/webhook` → `createWebhookRequest`,
retried by Quartz), not Kafka.

## Data (`gogovan` MySQL, MyBatis)
Three datasources: `gogovan`, `gogovansms2`, `gogoaudit`. Core tables:
`orderrequest`, `order`, `orderpool`, `orderamount`, `orderhist`, `orderowner`,
`orderpaymentinfo`, `orderflag`, `waypoint`, `appliedcoupon`, `appliedextra`, `driver`,
`user`, `organization`, `vehicle`/`vehiclepool`, `branch`, `coupon`/`mastercoupon`,
`vworderforda`.

## Core flow — driver order state transition (this service's signature job)
driver app / driver-service → `POST /api/order/accept/driver` (apikey) →
`orderService.driverAcceptOrder` updates `order`/`orderpool` state in `gogovan` DB;
`/release/driver` → `driverReleaseOrder`. `POST /api/order/webhook` (header
`orderRequestID`) → build B2B JSON → persist webhook request (Quartz-retried) — how
order updates reach external B2B partners. Pricing via the Go `fares` service, writing
`orderamount`.

## Review flags
Committed static AWS keys and secrets in `src/main/resources/<env>/*.properties.xml`;
legacy Spring 4.2 / JDK 8 (dated dependency surface).
