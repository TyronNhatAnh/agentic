# ggx-kr-ui (TypeScript monorepo) — the frontend SDK, not an app

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release branches
`releases/DAPro-2.13x`. Turborepo + pnpm; publishes to the GitHub packages registry
under `@gogovan/`. **It deploys nothing** — but it is the single place where the
KR admin frontend's view of the backend is defined, so read it when you need to know
which gateway endpoint a screen actually hits.

## Packages
| Package | Purpose |
|---|---|
| `@gogovan/kr-model` | Backend-agnostic domain types (order, user, driver, …) |
| `@gogovan/kr-backend-spi` | Abstract service interfaces (`IOrderService`, `IAuthService`, …) |
| `@gogovan/kr-backend` | The **Korea implementation** of those interfaces — the real endpoint list |
| `@gogovan/kr-ui-kit` | React component library (Shadcn + Tailwind, tsup ESM+CJS, Storybook + VRT) |
| `@gogovan/eslint-config-kr` | Shared ESLint/TS/Tailwind config |

Dependency direction: `ui-kit` and `backend` → `model`; `backend` implements
`backend-spi`.

## The backend package's layering
`stubs/` (raw API JSON types) → `from-stubs/` (stub → model) / `to-stubs/`
(model → request) → `services/` (SPI implementations) → `fetcher.ts` → `index.ts`
(`BackendKr`, the factory consumers construct). Adding an integration means walking
that chain in order.

`fetcher.ts` is a thin `fetch` wrapper: it resolves paths against the gateway base
URL, injects `Authorization: Bearer <token>` when a token is passed, and forces
`cache: "no-cache"` with `credentials: "include"`. **Trap:** it does not check
`response.ok` — it returns `await response.json()` unconditionally, so an HTTP error
surfaces as a parsed error body, not a thrown exception.

## Endpoint surface (the useful part)
Paths are gateway-prefixed; each prefix is one backend service.

- **order-service** `/order/api/v1/admin/orders*` (list, column-preferences, cancel,
  release, bulk cancel), `/order/api/v1/admin/daily-order-report/*`,
  `/order/api/v1/admin/report/statement-of-use/email`,
  `/order/api/v1/report/statement-of-use-driver/*`,
  `/order/api/v1/report/b2b-tracking-service/*` (`services/b2b/report.ts`).
- **user-service** `/user/api/v1/users/me`, `users/search`, `organization/search`,
  `branch/search`, and the whole RBAC set `admin/{role,roles,permissions,menus/tree,departments}`.
- **driver-service** `/driver/api/v1/driver/search`, `driver/get-by-order-radius`,
  `driver-report/*`, `vehicles/vehicle-pools`.
- **common-service** `/common/api/v1/vehicles/search`.
- **report-service** `/report/api/v1/order-bulk/{validate,submit,template}`.
- **ai-admin-assistant** `/ai-admin/api/v1/chat`, `/ai-admin/api/v1/history*`,
  `/ai-admin/api/v1/email-intake`.

`services/auth.ts` calls nothing — `isAdmin()`, `isB2BAdmin()`, `isSystemAdmin()` and
`isTokenExpired()` all decode the JWT locally. Token expiry is compared in **seconds**
because the backend issues `exp` in seconds.

## Consumers
[ui-admin.md](ui-admin.md) only. `ggx-kr-consumer-web` deliberately does **not** use
this SDK — it has its own axios clients (see [consumer-web.md](consumer-web.md)), so a
model change here does not reach the consumer app.

## Working on it
Versions move together (`0.5.x`) via Changesets — do not hand-bump. To test against a
consuming app: `pnpm turbo build --filter=@gogovan/kr-model` here, then `pnpm
local:link` in the consumer. Patching a missing API field with an `as unknown as` cast
in the consuming app instead of fixing the model here is the standing anti-pattern.
