# ggx-kr-consumer-cms (Next.js 14, App Router) — the public marketing site

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release branches
`releases/DAPro-2.13x`. Tailwind; `basePath`/`assetPrefix` `/introduction`.

## Owns
The GoGoX Korea landing/introduction page — effectively a single route,
`src/app/(landing)/page.tsx`. It is a brochure: **no backend calls, no auth, no
database, no state**. Its only outbound links are env-configured deep links into
[consumer-web](consumer-web.md): `NEXT_PUBLIC_CW_URL`, `NEXT_PUBLIC_CW_SIGNUP_URL`,
`NEXT_PUBLIC_CW_LOGIN_URL`. Google Analytics via `@next/third-parties`.

## Inbound
Port 3000 at host `gogox.co.kr` (`k8s-deployments/argo-ggx-kr-consumer-cms`), served
under `/introduction`. **Trap:** the `basePath` also pushes `public/robots.txt` and
the generated sitemap (`src/app/sitemap.ts`) under `/introduction`, and Next refuses a
rewrite out of a `basePath` — the apex robots/sitemap URLs therefore have to be handled
outside the app (see the note in `next.config.js`).

## Why it matters to a ticket
Almost never. If a ticket says "the CMS", check whether it means this brochure site or
the **admin** CMS screens, which live in [ui-admin](ui-admin.md). The name collides.
