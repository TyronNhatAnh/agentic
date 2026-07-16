# DB `gogovan` — bản đồ bảng cho `db_query` / `db_query_prod`

Một MySQL schema `gogovan` duy nhất, dùng chung cho order-service, da-api và các
service KR khác (~280 bảng live, chưa kể bảng `_bk_*`/archive). Đây là DB legacy:
tên bảng **không** theo convention của code hiện đại, nên suy tên bảng từ tên
model/struct sẽ trượt — model `OrderRequest` (Rails) trỏ tới bảng `orderrequest`,
không phải `order_requests`. Doc này cho cái khung để suy đúng; chi tiết thì
introspect trực tiếp (DB live là source of truth, doc chỉ là bản đồ).

## Naming

- Bảng legacy (đa số): **chữ thường, dính liền, số ít** — `orderrequest`,
  `orderamount`, `vehiclepool`, `commoncode`.
- Cột legacy: **PascalCase** — `ID`, `UserID`, `StatusCD`, `CreatedAt`, `DeletedAt`.
- Bảng/cột thêm về sau lẫn snake_case: `api_users`, `extra_prices`,
  `external_driver`, cột `kakao_id`, `lock_version`… Không đoán được thì
  `SHOW TABLES LIKE '%…%'` / `SHOW CREATE TABLE <t>`.
- `order` là reserved word — viết `` `order` ``.
- Soft delete phổ biến: lọc `DeletedAt IS NULL`.
- Cột `*CD` (StatusCD, PriceCD, TypeCD…) là mã tra qua bảng `commoncode`.

## Bảng core (cột verify từ staging 2026-07-16)

| Bảng | Là gì | Cột hay dùng |
|---|---|---|
| `orderrequest` | 1 booking của khách — "đơn hàng" theo nghĩa user nói | `ID`, `UserID`, `StatusCD`, `VehiclePoolID`, `PayCD`, `AppointmentAt`, `WaypointCount`, `FromPlace`/`ToPlace`, `CompletedAt`, `CancelledAt` |
| `` `order` `` | lượt gán driver cho 1 orderrequest (reassign → nhiều row) | `OrderRequestID`, `DriverUserID`, `StatusCD`, `PickupAt`, `CompletedAt`, `ReleasedAt`, `ReleaseReasonCD` |
| `orderamount` | line item giá của orderrequest | `OrderRequestID`, `PriceCD`, `TargetCD`, `Amount`, `Title` |
| `waypoint` | điểm dừng của orderrequest, thứ tự theo `Arrangement` | `OrderRequestID`, `Arrangement`, `StatusCD`, `AddressID`, `LocationLat`/`Lon`, `ReachedAt` |
| `orderowner` | snapshot org/branch/người đặt tại thời điểm tạo đơn | `OrderRequestID`, `OrganizationID`, `BranchID`, `UserID` |
| `user` | cả customer lẫn driver, phân bằng `TypeCD` | `ID`, `TypeCD`, `Email`, `PhoneNumber`, `OrganizationID`, `StatusCD` |
| `driver` | extension của user-là-driver, key `UserID` | `UserID`, `DriverLevelID`, `LocationLat`/`Lon`, `CreditAmount`, `OnWorkAt` |
| `organization` / `branch` | công ty B2B / chi nhánh | `ID`, `Name`, `OrganizationCode`, `BusinessLineCD` |
| `vehiclepool` | loại xe/dịch vụ hiển thị cho user chọn | `ID`, `Name`, `Title`, `VehicleID`, `PoolID`, `CommissionRatio` |
| `pricing` / `pricingset` | cấu hình pricing engine theo set | `PricingSetID`, `PriceCD`, `PriceID`, `IncludeExclude` |
| `commoncode` | bảng tra mọi mã `*CD` | `ColumnFullName`, `Value`, `Name`, `Title`, `ParentID` |

Các view `vw*` (`vworderforda`, `vworderlistforadmin`, `vwexport*`…) là view dựng
sẵn cho DA app / admin / report — khi cần đúng dữ liệu một màn hình đang hiển thị,
query thẳng view đó thường nhanh hơn tự join lại.

## Introspect

```sql
SHOW TABLES LIKE '%coupon%';
SHOW CREATE TABLE orderrequest;
SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns
WHERE table_schema='gogovan' AND COLUMN_NAME LIKE '%External%';
```
