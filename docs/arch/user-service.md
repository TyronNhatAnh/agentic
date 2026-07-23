# user-service (Go) — identity, auth, org, RBAC

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`.
Gin + gRPC, shared `gogovan` MySQL. Module `github.com/gogovan/ggx-kr-user-service`.

## Owns
User accounts (B2C/B2B/social), auth (JWT, OTP, social login), organizations &
branches (B2B hierarchy), admin RBAC (roles/departments/menus/permissions),
agreements/ToS, feature flags, payment client-tokens, KCB real-name (SSN/DI)
verification.

## Inbound
- HTTP: `/api/v1/{auth,users,organization,branch,admin,guest,client-token,feature}`.
  Router `internal/api/http/v1/routes.go`.
- gRPC: `UserService`, `OrganizationService`, `DevicePushKeyService`,
  `UserDriverService`, `AdminUserService` (`internal/api/grpc/grpc_server.go`).

## Calls out (all sync gRPC unless noted)
- **common** — `GetConfigurationByKey` (config), `VerifyOtp`, `SaveActionLog`,
  `GetCommonCode`.
- **notification** — `OtpService.SendOTPSMS` (forgot-password),
  `KakaoTalkService.SendKakaoTalkMessage` (OTP).
- **order** — `VerifyBizRegistrationNumber` (etax/B2B), `GetActiveOrderCountByUserId`
  (withdrawal guard), `AddOrgPricing` (org creation pushes pricing).
- **legacy business backend** — HTTP (`http.DefaultClient`) → `business-staging.gogovan.co.kr`
  (`internal/infrastructure/external_service/dapro/da_service.go`), for KCB handshake
  and `/login-da`.
- **External:** Apple/Kakao/Naver/Google OAuth; **KCB ok-name** real-name API
  (`external_service/kcb/`). Vault, Slack.

## Async
None — Kafka code is commented out / dead boilerplate. Redis = token/cache store.

## Data (`gogovan` MySQL)
`user`, `authentication`, `organization`, `branch`, `business`, `userpool`,
`adminuser`, `adminroles`, `admindepartments`, `menus`, `devicepushkey`,
`featureflag`, `kcb_*`, `paymentinfo`. Read/write DB split.

## Core flows
- **Social login:** `/auth/login-by-{kakao,naver,google}` → provider service validates
  token → look up/create `user`+`authentication`, issue JWT.
- **OTP:** `/auth/otp-request` → notification `KakaoTalkService`/`OtpService`; verify
  via common `VerifyOtp`.
- **KCB / DI linkage:** `/auth/kcb/*`, `/guest/kcb/ssn/verify` → ok-name API + legacy
  DA web API; match/link users by DI, write history, log via common `SaveActionLog`.
- **B2B signup:** biz-reg verified via order `VerifyBizRegistrationNumber`; org pricing
  pushed to order `AddOrgPricing`.
