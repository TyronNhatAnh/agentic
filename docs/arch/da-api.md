# da-api (Ruby/Rails) — Driver App backend

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`.
Rails 6. Reads/writes `gogovan` MySQL directly via ActiveRecord.

## Owns
Driver-app flows: auth/signup/KCB, order feed & lifecycle actions (assign/pickup/
release/complete), delivery history, e-tax, document/ePOD upload, notifications, KPI.
An adapter between the driver mobile app and both the legacy Java backend and the Go
services. It is a **client**, not a callee, of the Go services (see the "DA trap" in
the index — the Go services' `DaService` is NOT this repo).

## Inbound (flat, unversioned Rails routes — `config/routes.rb`)
`/guest/*` (login/signup/OTP/KCB/banks/vehicles), `/accounts/*`, `/orders`,
`/orders/:id/{assign,pickup,release}`, `/orders/complete_order`, `/pieces/:id/pickup`,
`/delivery_history`, `/etax_orders`, `/documents/*`, `/notifications/*`,
`namespace :kakao_account|:apple|:ggt`. Reached via Kong under `/da-api`
(evidence: KCB return_url `stag-api.gogox.co.kr/da-api/guest/kcb/...`,
`config/kcb_controller.yml`).

## Calls out (all Faraday HTTP)
- **legacy business backend** (`business.gogovan.co.kr`) — `api/order/accept/driver`,
  `api/order/release/driver`, `api/order/webhook`, `/api/sms/send`, etax + biz-reg.
  Auth `apikey = SHA256("9090van"+driver_id)`. `app/services/{order,account,business}_service.rb`.
- **Go services via Kong** (`api.gogox.co.kr/{service}`): user (KCB config/mock,
  `/user/api/v1/auth/login-da`), payment (bank verify-requests/revoke), driver
  (Firestore location POST), common (audit-log, HMAC `secret`). Clients in
  `app/services/*_client.rb`, config `config/{endpoint,driver_service,common_service}.yml`.
- **External:** FCM (push), MessageBird (SMS), AWS Kinesis via API Gateway (GoGoTrack
  location), S3, Salesforce ("SF international" order sync).

## Async
No Sidekiq/Kafka — in-process `Concurrent::Async` threads (`service.async.method`)
for webhooks, SF sync, FCM, GGT. Redis = assign-order distributed lock + cache.

## Data (`gogovan` MySQL via ActiveRecord)
`Order`→`order`, `OrderRequest`→`orderrequest`, `OrderAmount`→`orderamount`,
`Driver`→`driver`, `User`→`user`, `PaymentInfo`→`paymentinfo`, `DepositHist`→
`deposithist`; read views `VwOrderForDriver`→`vworderfordriver`,
`VwAvailableOrders`→`vwavailableorders`. ~73 models total.

## Core flows
- **Assign/accept:** `PUT /orders/:id/assign` → `OrderService#assign_order`: guards +
  Redis lock → POST `api/order/accept/driver` to the **legacy business backend**.
- **Complete:** `POST /orders/complete_order` → DB txn (`orderrequest.StatusCD=3`,
  `Order.CompletedAt`, create `DepositHist`) → async webhook + Salesforce sync.
- **Signup + identity/bank:** delegate SSN/bank verify to user-service /
  payment-service; write every change to common-service audit log.
