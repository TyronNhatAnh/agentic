# api-layer (Ruby/Rails) — customer-app BFF

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.127`,
org `gogovan-korea`. Rails **5.2** API-only (Ruby 2.6, Puma). README: "a layer over
the existing APIs used by the Java apps."

## Owns
A **BFF/façade for the GoGoX KR customer mobile app** (the "CA" app + B2C web). It
presents mobile-friendly REST endpoints and translates them into calls to web-api
(Java) and the Go services — request/response reshaping, version-gated behavior, i18n
error strings, price recompute. It owns **no** core order/payment/user logic.

## Inbound (`config/routes.rb`, flat/unversioned)
`account/`, `location/` + `locationw3w/`, `order/` (estimate/submit/status/cancel/
history/coupons), `home-moving/order/*` + `home_moving/`, `vehicles/`, `push/`,
`payment/` (card list/register/select), `ratings/`, `ads`, `kakaotalk/webhook|send`,
and a parallel `b2b/` namespace. `PaginateConstraint` caps page at 50.

## Calls out (all sync Faraday HTTP)
- **web-api** (legacy Java, `business.gogovan.co.kr/api`) — `/api/order/{price,submit,
  get}/b2c`, `/api/payment/*`, `/api/ratings*`, `/api/service/location|address*`.
  Auth: SHA256 `9090van`-salted apikey (`app/services/api_service.rb`, `.../b2_c.rb`).
- **Go order-service + user-service** via Kong (`api.gogox.co.kr`) — `/order/api/v1/
  orders`, `/user/api/v1/auth/login`, `/users/me`. Auth: Bearer JWT.
  `app/services/api_service/b2_c_new.rb`, `account_service.rb`.
- **common** (home-moving), **notification** (KakaoTalk) Go services via Kong.
- **Mid-migration:** `b2_c.rb` (→web-api) and `b2_c_new.rb` (→Go order) coexist,
  version-gated — the customer-app equivalent of the monolith→Go split.

## Async / Data
No Sidekiq/Kafka — all downstream I/O synchronous. Dual DB via `multiverse`:
- **Postgres** (`api_production`) — the only DB it writes: `ApiUser` (API tokens,
  per-user extra-price/vehicle-option overrides).
- **`gogovan` MySQL** — read-only (`GogovanRecord.readonly? → true`); 25/33 models read
  `user`/`order`/`order_request`/`organization`/`vehicle_pool` directly.

## External
JWT (customer auth) + legacy `ApiUser` token path (B2B). AWS S3 (request images),
what3words, KakaoTalk (via notification), weather. Bugsnag/New Relic/Slack.

## Core flows
- **Order estimate/submit:** `order_controller` → `B2C` (`b2_c.rb`) → web-api
  `/api/order/{price,submit}/b2c`, response reshaped by `OrderService` (versioning +
  Korean error mapping); newer `b2_c_new.rb` → Go order-service, mapping vehicle
  name→`vehiclePoolId` (read from `gogovan` MySQL). Façade, not owner.
- **Login/whoami:** → Go user-service `/auth/login`; later requests decode JWT locally
  + resolve the `user` row from `gogovan` MySQL.
