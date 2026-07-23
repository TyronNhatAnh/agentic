# web-systemAdmin (Java) — super-admin / system console

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.123`,
org `gogovan-korea`. `com.gogovan.sysadmin:system` (WAR), Spring MVC **3.2**, MyBatis,
JSP/Tiles, JDK 8. Access gated to `IsSystemAdmin` users.

## Owns
The internal **super-admin console for developers/system admins** — low-level platform
config web-admin doesn't do: dynamic table/column metadata & rendering config
(`CommonTable`/`CommonColumn`/`ColumnStyle`), the console's menu tree, config key/value
store, environment tokens (IP/branch API tokens), integration request logs, error
dumps, and **direct MySQL process management (`SHOW PROCESSLIST`/`KILL`)**.

## Inbound (JSP/Tiles, no JSON/REST)
`/dashboard`, `/login` + `/sociallogin/google` (id/pw login disabled),
`/adminuser` (iframe wrapper) + deprecated `/adminuser-legacy`, `/common/table*`,
`/new/menu*`, `/menu`, `/category`, `/pool`, `/meta`, `/bank`, `/config`, `/token`,
`/actionlog`, `/errordump`, `/process`, `/system/{reloadhash,encrypt}`, `/platform`.

## Calls out — almost none
- **Google OAuth** (`SocialLoginController`) — token/tokeninfo to resolve login email.
- **web-admin** — only as a **browser iframe** (`web.business.gogovan.co.kr/admin/
  roles-permission/?displayMode=iframe`), not a server call. The RBAC-editing UI is
  embedded from web-admin.
- **No** RestTemplate/Feign/Go-service/web-api client (grep empty except Google).

## Admin RBAC ownership (the useful fact)
Split, mid-migration. **Data + enforcement live here + in `gogovan` MySQL**: login reads
`adminuser` + `adminrolemenupermissions` (`CONCAT(menu.Code,':',read|write)`) +
`adminuseraccountgroup`, mints a JWT cookie (`JWebToken`), refreshed from DB on expiry.
The role/permission **editing UI** is delegated to web-admin's iframe; the in-app editor
is deprecated. **user-service is NOT the source of admin-console RBAC** (no calls to it).
HYPOTHESIS: admin RBAC is consolidating into web-admin's UI while `gogovan` tables remain
system of record.

## Async / Data
No Kafka/scheduler. Direct `gogovan` MySQL via MyBatis (`sqlGogoVanConnection`) + a
separate audit DB (`sqlGogoAuditConnection`, prod schema `dev_gogovan`) for `ActionLog`
(before/after JSON). Tables: `adminuser`, `adminrolemenupermissions`,
`adminuseraccountgroup`, `Menu*`, `Pool`, `CommonTable`/`CommonColumn`/`ColumnStyle`,
`Configuration`, `Token`, `ApiRequest`, `ErrorDump`, + live `SHOW PROCESSLIST`.

## Review flags (notable)
- **SQL-injection surface:** raw `${item.Name}` / `LIKE '%${item.Value}%'` interpolation
  in dynamic filter SQL (`ActionLog.xml`, `Common.xml`, `AdminUser.xml`).
- EOL stack: Spring 3.2.18, `DefaultHttpClient`, MySQL connector 5.1.39 (unpatched).
- Auth JWT cookie set with `setHttpOnly(false)` (XSS-exposed).
