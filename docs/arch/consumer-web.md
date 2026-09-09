# ggx-kr-consumer-web (Next.js, Pages Router) — the customer web app

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). antd 5 + Tailwind,
next-i18next (`ko` default), SWR for data, Redux Toolkit for state.
`basePath: "/cw"`. Browser client only — no database of its own.

**Read this first:** the B2B half of this app is **not on any release branch**.
`src/lib/apis/b2b/*`, `components/route-guard/guard.ts` and
`helpers/axios/helpers/unauthorized.ts` exist only on the feature line
(`feature/KRP-11887-b2b-order-flow`), not on `origin/releases/DAPro-2.134`. Everything
below marked B2B describes unreleased work; the B2C sections are what production runs.

## Owns
Two products in one app:
- **B2C** (production): signup/login incl. Kakao/Naver/Google OAuth, order placing
  (`/orders/new`), payment, order list & detail, coupons, profile/withdraw,
  driver e-tax, terms pages.
- **B2B** (pilot, `/cw/b2b/*`): its own login, order creation and payment.

Plus two public unauthenticated tracking pages:
`/public/customer/[organization_id]/order/[order_id]` and `/public/mobis/[order_id]`.

## Inbound
Port 3000, two ingress hosts (`k8s-deployments/argo-ggx-kr-consumer-web`):
- `cw.gogox.co.kr` (stag `stag-cw.gogox.co.kr`) — the app, under `/cw`.
- `mobis.gogox.co.kr` (stag `stag-mobis.gogox.co.kr`) — **the same deployment**.
  `src/middleware.ts` matches `host.includes("mobis")` and rewrites every path to
  `/public/mobis<path>`, so the Mobis partner sees only the tracking page. **Trap:**
  it reads `x-forwarded-host` first — if the proxy drops that header the rewrite stops
  firing and Mobis users land on the B2C app.

## Calls out
Its **own** axios clients, not the `@gogovan/kr-backend` SDK
(`src/lib/common/helpers/axios/index.ts`) — one instance per gateway prefix, all
based on `NEXT_PUBLIC_BACKEND_API_URL` (Kong):

| Client | Prefix | Service |
|---|---|---|
| `userServiceApiClient` | `/user/api/v1` | [user-service](user-service.md) |
| `orderServiceApiClient` | `/order/api/v1` | [order-service](order-service.md) |
| `driverServiceApiClient` | `/driver/api/v1` | [driver-service](driver-service.md) |
| `commonServiceApiClient` | `/common/api/v1` | [common-service](common-service.md) |
| `notificationServiceApiClient` / `notifyServiceApiClient` | `/notification/api/v1` | [notification-service](notification-service.md) |
| `driverDAServiceApiClient` | `/da-api/guest/driver` | [da-api](da-api.md), unauthenticated guest route |

API wrappers live in `src/lib/apis/*.ts` (`b2b/order.ts` for the pilot). Interceptors
add the bearer token (`interceptors/auth.ts`), parse the envelope
(`response-parser.ts`), attach tracking headers, and handle 401 through the pure
`helpers/unauthorized.ts`.

Third parties in the browser: **Toss BrandPay** (`src/lib/components/toss-brandpay/`)
for card payment — settled server-side by [payment-service](payment-service.md);
Kakao/Naver/Google OAuth; Firebase (web push).

## B2C / B2B isolation (load-bearing convention)
B2C is in production, B2B is a pilot, so B2B screens are a **separate copied tree**
under `containers/b2b/**`. Branching inside a B2C file — `if (isB2B)`, reading the
route or `typeCd`, or a config flag — is banned; the duplication is deliberate
isolation. The only shared-and-aware code is the one-app infrastructure: the route
guard (`components/route-guard/guard.ts`) and the axios 401 handler
(`helpers/axios/helpers/unauthorized.ts`), which must stay pure functions with their
own tests. B2B estimate/create call the bare `/estimate` and `/orders` endpoints
because the `/order/api/v1/b2b/orders*` group **has not been written yet** (BE-6) —
not merely undeployed; see `src/lib/apis/b2b/order.ts`.

## Data
No DB, no queue, no cache tier. Service worker built at build time
(`scripts/service-worker-build.js`) for push. `MAINTENANCE_MODE` env gates the app.
