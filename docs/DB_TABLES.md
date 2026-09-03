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
- Bảng/cột thêm về sau lẫn snake_case: `api_users`, `external_driver`,
  `extra_prices`, cột `kakao_id`, `lock_version`… Không đoán được thì
  `SHOW TABLES LIKE '%…%'` / `SHOW CREATE TABLE <t>`.
- Bảng snake_case mới và bảng legacy cùng chủ đề **không** thay thế nhau: phụ phí
  legacy nằm ở `extraprice` (2233 row, có ở cả hai env), còn `extra_prices`
  (snake_case, 14 row) **chỉ có trên staging**. Xem §Staging ≠ prod.
- `order` là reserved word — viết `` `order` ``.
- Soft delete phổ biến: lọc `DeletedAt IS NULL`.
- Cột `*CD` (StatusCD, PriceCD, TypeCD…) là mã tra qua bảng `commoncode`.

## Bảng core (cột verify từ staging 2026-08-12)

Cột dưới đây là **cột hay dùng, KHÔNG phải toàn bộ cột** của bảng — mỗi bảng còn
hàng chục cột nữa. Cột vắng mặt ở đây ≠ cột không tồn tại, và ngược lại cũng
không có nghĩa là tên bạn nghĩ ra sẽ có thật. Cần một cột không nằm trong bảng
này thì `SHOW CREATE TABLE <t>` **trước**, đừng select thẳng rồi thử tên biến thể
khi trượt — legacy hay gắn hậu tố `CD`/`ID` (`PlatformCD` chứ không phải
`Platform`, `AdminUserID` chứ không phải `CreatedBy`).

| Bảng | Là gì | Cột hay dùng |
|---|---|---|
| `orderrequest` | 1 booking của khách — "đơn hàng" theo nghĩa user nói | `ID`, `UserID`, `StatusCD`, `VehiclePoolID`, `PayCD`, `AppointmentAt`, `WaypointCount`, `FromPlace`/`ToPlace`, `CompletedAt`, `CancelledAt` |
| `` `order` `` | lượt gán driver cho 1 orderrequest (reassign → nhiều row) | `OrderRequestID`, `DriverUserID`, `StatusCD`, `PickupAt`, `CompletedAt`, `ReleasedAt`, `ReleaseReasonCD` |
| `orderamount` | line item giá của orderrequest | `OrderRequestID`, `PriceCD`, `TargetCD`, `Amount`, `Title` |
| `waypoint` | điểm dừng của orderrequest, thứ tự theo `Arrangement` | `OrderRequestID`, `Arrangement`, `StatusCD`, `AddressID`, `LocationLat`/`Lon`, `ReachedAt` |
| `goodsinfo` | hàng hóa giao/nhận tại **1 waypoint** — không phải cột của orderrequest | `WaypointID`, `Name`, `Quantity`, `Weight`, `Length`/`Width`/`Height`, `TypeCD`, `PriceCD`, `StatusCD`, `DeliveredAt`, `ExternalGoodsID` |
| `orderowner` | snapshot org/branch/người đặt tại thời điểm tạo đơn | `OrderRequestID`, `OrganizationID`, `BranchID`, `UserID` |
| `user` | cả customer lẫn driver, phân bằng `TypeCD` | `ID`, `TypeCD`, `Email`, `PhoneNumber`, `OrganizationID`, `StatusCD` |
| `business` | hồ sơ thuế/đăng ký KD gắn theo `UserID` — MST **không** nằm trong `user` | `UserID`, `BizRegistrationNumber`, `ResidentRegistrationNumber`, `TaxPlayerCD` |
| `extraprice` | phụ phí legacy (bảng dùng thật ở prod) | `ID`, `Name`, `Title`, `TypeCD`, `VehiclePoolID`, `RegionID`, `DeletedAt` |
| `driver` | extension của user-là-driver, key `UserID` | `UserID`, `DriverLevelID`, `LocationLat`/`Lon`, `CreditAmount`, `OnWorkAt` |
| `organization` / `branch` | công ty B2B / chi nhánh | `ID`, `Name`, `OrganizationCode`, `BusinessLineCD` |
| `vehiclepool` | loại xe/dịch vụ hiển thị cho user chọn | `ID`, `Name`, `Title`, `VehicleID`, `PoolID`, `CommissionRatio` |
| `pricing` / `pricingset` | cấu hình pricing engine theo set | `PricingSetID`, `PriceCD`, `PriceID`, `IncludeExclude` |
| `commoncode` | bảng tra mọi mã `*CD` | `ColumnFullName`, `Value`, `Name`, `Title`, `ParentID` |

Các view `vw*` (`vworderforda`, `vworderlistforadmin`, `vwexport*`…) là view dựng
sẵn cho DA app / admin / report — khi cần đúng dữ liệu một màn hình đang hiển thị,
query thẳng view đó thường nhanh hơn tự join lại.

## Lịch sử & truy vết

Bảng ở §Bảng core đều là **current-state**: chúng trả lời "giá trị đang là gì",
không trả lời "tại sao thành thế". Câu hỏi dạng *tại sao đơn này ra số tiền đó*,
*ai đổi cái này*, *lúc đó dữ liệu là gì* thì tra hai bảng dưới **trước**, đừng
kết luận từ current-state rồi suy ngược bằng đọc code — số hiện tại có thể đã bị
sửa tay sau đó, và code không cho biết snapshot lúc chạy là gì.

| Bảng | Là gì | Cột hay dùng |
|---|---|---|
| `orderhist` | snapshot đơn tại **mỗi lần thay đổi** — nguồn duy nhất cho "tiền đổi lúc nào, ai đổi" | `OrderRequestID`, `TypeCD`, `Status`, `Description`, `CreatorCD`, `CreatorUserID`, `CreatedAt`, `Meta` |
| `auditlog` | audit log chung, do common-service ghi qua gRPC `SaveAuditLog` | `EntityType`, `EntityID`, `Action`, `ActorID`/`ActorType`/`ActorName`, `Platform`, `Reason`, `BeforeData`, `AfterData`, `OccurredAt` |

**`orderhist.Meta`** là JSON; khoản tiền nằm ở key `AmountList` =
`[{ID, OrderRequestID, Title, Amount}]`. Chỉ có `Title` (text tiếng Hàn, ví dụ
`소득세(기사)`) — **không có `PriceCD`**, nên lọc một khoản phải match theo `Title`,
không map được bằng mã như ở `orderamount`.

**`auditlog` là controlled vocabulary, không phải log toàn hệ thống.** Chỉ 6
`EntityType` được đăng ký trong catalog: `DriverBankAccount`, `DriverSSN`,
`DriverBusinessInfo`, `OrganizationBusinessInfo`, `UserPoolPermission`,
`OrganizationPoolAssignment` — **toàn bộ là identity/permission**. Không có
`orderamount`, `order`, pricing. Nghĩa là **thay đổi tiền không hề có audit log**:
một khoản bị sửa tay ở admin sẽ không xuất hiện ở đây, chỉ để lại vết ở
`orderhist`. Đừng kết luận "không ai sửa" vì `auditlog` trống.

Chi tiết khác: `BeforeData` NULL khi `Action=CREATE`, `AfterData` NULL khi DELETE
(dùng để phân biệt tạo mới với sửa). `Platform` ∈ `DA|CA|WEB2|ADMIN|B2B|B2C|SYSTEM`,
NULL cho row legacy. `ActorType` ∈ `DRIVER|ADMIN|B2C_USER|B2B_USER|SYSTEM`. Field
nhạy cảm được AES-256-GCM (`IsEncrypted`/`EncKeyVersion`). Bảng bắt đầu có dữ liệu
từ giữa 2026 — hỏi về mốc cũ hơn thì kiểm `MIN(OccurredAt)` trước khi kết luận
"không có thay đổi".

`actionlog` là bảng chị em cùng do common-service ghi, **chưa map** ở doc này.

## Staging ≠ prod

`db_query` (staging) và `db_query_prod` là **hai schema khác nhau**, không phải hai
bản sao. Introspect ở env này rồi query env kia là cách trượt phổ biến nhất:

- Bảng ad-hoc `bak_*` / `*_bk_*` / `*_tmp` do người tạo tay khi vá dữ liệu — sinh
  ở env nào thì chỉ có ở env đó.
- Bảng snake_case mới có thể đã lên staging mà chưa lên prod (`extra_prices`).

Định query prod thì `SHOW TABLES` / `SHOW CREATE TABLE` bằng chính `db_query_prod`.

## Introspect

```sql
SHOW TABLES LIKE '%coupon%';
SHOW CREATE TABLE orderrequest;
SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns
WHERE table_schema='gogovan' AND COLUMN_NAME LIKE '%External%';
```
