# ggx-kr-ui-admin (Next.js 14) — the new admin/system console SPA

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release branches
`releases/DAPro-2.13x`. **Not** the legacy Java admin — that is
[web-admin.md](web-admin.md), a separate app on a separate host. This one is a
browser client only: it owns no database and every call goes out through the Kong
gateway.

## Owns
The React/Next replacement for parts of the admin panel: order list, order
control/dispatch, B2B bulk order upload, daily-order / driver / finance reports,
coupons, roles & permissions, the AI chat panel and the Mobis email-intake screen.
Routes live under `src/app/[locale]/` — `[locale]` is always `kr` (Seoul timezone,
next-intl, all strings in `messages/kr.json`).

## Inbound
Served on port 3000 behind the shared KR ingress
(`k8s-deployments/argo-ggx-kr-shared/templates/ingress-backend.yaml`) at host
`web.business.gogovan.co.kr` (stag: `web.business-staging.gogovan.co.kr`):
- `/admin/*` → this service. Next `basePath`/`assetPrefix` are hardcoded `/admin`
  (`next.config.js`), so the app only works under that prefix.
- `/system/*` → **the same service**, for the system-admin tier.
- `/b2b/*` → `ggx-kr-ui-b2b`, a *frozen separate deployment* — see the note below.

Its own `/api/health` (`src/app/api/health/route.ts`) is the only server route; the
middleware matcher excludes `/api`.

## Calls out
Everything through **`@gogovan/kr-backend`** (the SDK in
[kr-ui.md](kr-ui.md)) pointed at `NEXT_PUBLIC_BACKEND_API_URL`
(`https://stag-api.gogox.co.kr` / the prod Kong host). Never call an API directly —
`src/lib/helpers/backend.ts:createBackend()` is the only constructor. Gateway
prefixes reached: `/order/api/v1/admin/*`, `/user/api/v1/admin/*`,
`/driver/api/v1/*`, `/common/api/v1/vehicles/*`, `/report/api/v1/order-bulk/*`
(bulk upload → [report-service.md](report-service.md)) and `/ai-admin/api/v1/*`
(chat + email-intake → [ai-admin-assistant.md](ai-admin-assistant.md)).

## Auth
No session of its own. The JWT sits in a cookie (`NEXT_PUBLIC_AUTH_COOKIE_KEY`,
`access_token`) minted by the legacy login pages; `src/middlewares/auth.ts` runs
before every page and decodes it **client-side** through the SDK's `auth()` service:
`isTokenExpired()` → redirect to `NEXT_PUBLIC_LOGIN_PAGE_URL` (or
`..._SYSTEM_LOGIN_PAGE_URL`), then `isAdmin()` / `isSystemAdmin()` — both read
permission claims out of the JWT, no backend round-trip — gate admin vs system-admin
pages, failing to `/error/403`. **Trap:** the tiers are decided from JWT claims in the
browser, so the real authorization still has to be enforced by each service.

## Data / state
No database, no cache, no queue. Server components fetch per request
(`cache: "no-cache"` is the SDK default); client state is plain React Context +
hooks under `src/lib/contexts/` (no Redux). `displayMode=iframe` hides the header via
the `withIframeMode` HOC, which is how legacy pages embed these screens.

## ui-b2b — a deployment, not a repo
`k8s-deployments/argo-ggx-kr-ui-b2b` is a live Argo app at
`web.business.gogovan.co.kr/b2b`, and its `app_repo` annotation points back at
`gogovan/ggx-kr-ui-admin`. But `gogovan/ggx-kr-ui-b2b` does not exist on GitHub, this
repo's `basePath` is hardcoded `/admin` (it cannot build a `/b2b` app as configured),
and the image is pinned to `8ada059` from 2025-09-17 while ui-admin is on a 2026-08-12
build. Treat `/b2b` as **frozen legacy**, not current architecture. The only live trace
in source is `isB2BAdmin()` / `getLoginBtoBUser()` in the SDK's `auth` service.
