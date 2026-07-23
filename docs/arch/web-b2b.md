# web-b2b (Java) — B2B corporate web portal

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.128`,
org `gogovan-korea`. `com.gogovan.b2b:b2b` (WAR), Spring MVC **3.2**, JDK 8, JSP +
Apache Tiles. Served at `web.business.gogovan.co.kr/b2b`.

## Owns / shape
Server-rendered **B2B corporate customer portal** — corporate customers (and internal
call-manager staff) log in to place/manage delivery orders, manage branches/users/
addresses, and view finance/dashboard reports. Hybrid like web-admin: renders JSP +
**direct `gogovan` MySQL** access, while delegating order lifecycle to web-api. Not a
BFF, not stateless. **No Go/Kong calls at all** (grep found none).

## Inbound (JSP controllers + some `/api` JSON; `controller/`)
`OrderController` (`/`,`/order`, + JSON `/api/order/{submit,check,update}`,
`/api/capable/a2b`), `OrganizationController`, `UserController`, `AddressController`,
`WaypointController`, `ReportController` (Excel export), `DashboardController`,
`LoginController` (`/login`,`/login/menual`), `SignupController`, `AjaxController`.

## Calls out (sync `HttpURLConnection` via `Utility.download`, no Feign/RestTemplate)
Single backend target: **web-api** (`api.url` = `business.gogovan.co.kr/api`).
- `service/impl/InterfaceServiceImpl.java` — `/service/{location,extra/goods,capable/a2b}`,
  `/order/{get,price,submit,update,history}/b2b`, `/order/cancel/b2c`, `/coupon/list`.
- `OrderServiceImpl.java:649` — `/order/submit/admin` (call-manager path).
- `AjaxController.java` — `/service/address/search`, `/addressdetails/search`.
- Also: call-manager PHP (`218.153.209.226`), socket servers (`business...:55000/55011`),
  Kakao/Atlan maps, Gmail SMTP.

## Async / Data
No Kafka/scheduler/Quartz — only an ad-hoc cached thread pool for fire-and-forget
logging/mail. **Direct `gogovan` MySQL via MyBatis** (read/write master + read
replica): `user`, `orderrequest`, `orderamount`, `organization`, `organizationpricing`,
`branch`, `waypoint`, `address`, `vehiclepool`/`vehicleprice`/`extraprice`, `coupon`,
`deposithist`, `paymentinfo`, + report views `vworderlistforb`, `vwfinancereportforb`.
Audit logging writes via a `gogoaudit` MyBatis session (local, not the common-service
audit API).

## External
AWS S3 (profile/doc/image, keys hardcoded in properties). Auth: HTTP session +
hand-rolled HS256 JWT (`common/JWebToken.java`, cookie `b2b_access_token` scoped to
`.business.gogovan.co.kr` for SSO across the business.* domain). No PG integration —
deposits/payments are DB records + web-api pricing. Bugsnag.

## Core flows
- **Corporate order placement:** browser → `OrderController` → address/goods/capability
  via web-api `/service/*` → quote via web-api `/order/price/b2b` → submit via
  `/order/submit/b2b` (call-manager → `/order/submit/admin`). web-api owns persistence;
  web-b2b reads order lists/history back from `gogovan` DB + `/order/history/b2b`.
- **Account/billing/reporting:** organization/branch/user/pricing/deposit = pure MyBatis
  CRUD on `gogovan`; `ReportController` builds finance reports from DB views → POI Excel
  (no web-api hop).
