# notification-service (Go) — outbound messaging

Detail file for [the backend map](../GOGOX_ARCHITECTURE.md). Release `DAPro-2.130`.
Gin + gRPC, shared `gogovan` MySQL + Redis. A **sink**: order & user call it; it only
calls common.

## Owns
All outbound customer/driver messaging — FCM push, KakaoTalk (Alimtalk templates via
CJ MPlace, with a legacy `MSG_QUEUE` "GT Agent" fallback), SMS (MessageBird + Twilio),
email (SendGrid), and OTP generate/deliver/verify. Stores device push tokens + OTP
state (Redis).

## Inbound
- gRPC (`internal/api/grpc/grpc_server.go`): `NotificationService`
  (`SendFirebaseNotification`, `DeleteFirebaseToken`, …), `KakaoTalkService`
  (`SendKakaoTalkMessage`), `OtpService` (`SendOTPSMS`, `VerifyOtp`). These are what
  order & user call.
- HTTP: `/api/v1/{sms,email,firebase}` (JWT) + `/api/v1/guest/otp/*` +
  `/guest/kakaotalk/{webhook,send}`.

## Calls out
- **common** only — gRPC `GetConfigurationByKey` for message templates
  (`internal/application/otp/send_otp_sms/`). No order/user/driver clients.

## Async — Kafka present but DEAD
Consumer code exists (`internal/api/kafka/{consumer,otp_consumer}.go`) for topics
`gogovan.consumer-web.otp.{send,send-sms,send-email}` and `...notification.push`, but
**`Consumer.ProcessMessages` is never invoked** (defined `consumer.go:26`, uncalled)
and the push branch is commented out (`consumer.go:49-50`). Runtime delivery is
gRPC/HTTP only. No SQS/SNS/RabbitMQ.

## Data (`gogovan` MySQL, sqlx)
`CustomerNotify` (push tokens), `KakaoTalkRequest`, `kakaotalkwebhook`,
`kakaotalkresponse`, `MSG_QUEUE` (legacy KR SMS/Kakao gateway). Redis = OTP store
(hashed, TTL).

## External providers
Firebase Admin SDK (push; a legacy HTTP FCM sender also exists — likely unused),
CJ MPlace (KakaoTalk), MessageBird + Twilio (SMS), SendGrid (email).
**Review flag:** stag config appears to hold a hardcoded SendGrid API key — check for
leaked secrets when reviewing this repo's config.

## Core flows
- **Push (gRPC `SendFirebaseNotification`):** → `GetCustomerNotifyByUserId` → build
  Firebase message (APNS+Android) → send per token → prune failed tokens.
- **Send OTP SMS:** normalize phone → generate 6-digit → store hashed in Redis (TTL) →
  template via common `GetConfigurationByKey` → send via CJ MPlace or `MSG_QUEUE`.
- **Verify OTP:** read hashed OTP from Redis → compare → delete on success.
