# Setup — lấy key & chạy bot

Hướng dẫn end-to-end để dựng `agentic` từ con số 0: lấy đủ token (Slack, GitHub, Jira,
Notion, Grafana), điền `.env`, và chạy bằng Makefile.

Bot xác thực Claude qua `claude login` của host (OAuth, **không** dùng API key). Mọi
integration khác lấy credential từ `.env` — đọc một lần lúc khởi động qua
[config.py](../src/agentic/config.py). Token để trống thì tool tương ứng tự tắt và báo
lỗi `CONFIG` thay vì làm bot chết giữa chừng, nên bạn có thể bật dần từng integration.

---

## 0. Prerequisites

| Thứ cần | Cách kiểm tra | Ghi chú |
| --- | --- | --- |
| Claude CLI đã login | `claude --version` (≥ `MIN_CLAUDE_VERSION`, mặc định `2.0.0`) | `claude login` một lần trên host. Không có version ≥ min thì bot từ chối start. |
| Python 3.11+ | `python3 --version` | |
| `sqlite3` CLI | `sqlite3 --version` | Chỉ cần cho `make db-show` / `db-stats`. |

```bash
cd /Users/tyron/Projects/agentic
make install     # tạo .venv + editable install + copy .env.example -> .env
```

`make install` chỉ copy `.env` nếu chưa tồn tại. Sau đó mở `.env` và điền token theo các
mục dưới. Chỉ **Slack** là bắt buộc để bot chạy; phần còn lại bật theo nhu cầu.

---

## 1. Slack (bắt buộc)

Bot chạy **Socket Mode** nên cần 2 token: bot token (`xoxb-`) và app-level token (`xapp-`).

1. Tạo app tại <https://api.slack.com/apps> → **From scratch**.
2. **Socket Mode** → bật ON.
3. **OAuth & Permissions** → Bot Token Scopes tối thiểu: `app_mentions:read`, `chat:write`.
   Cài app vào workspace → copy **Bot User OAuth Token** (`xoxb-...`).
4. **Event Subscriptions** → bật, subscribe bot event `app_mention`. (DM bị cố tình bỏ qua.)
5. **Basic Information → App-Level Tokens** → tạo token scope `connections:write` →
   copy (`xapp-...`).

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
# Tùy chọn: chỉ cho phép một số channel (tên hoặc ID, phẩy ngăn cách). Để trống = mọi channel.
SLACK_ALLOWED_CHANNELS=da-ops,C0123ABCD
```

Sau khi chạy, `@mention` bot trong channel được allow. Lưu ý chỉ chạy **một** instance
Socket Mode — nhiều instance sẽ chia event ngẫu nhiên và rất khó debug.

---

## 2. GitHub

Dùng cho các tool `github_*` (đọc PR, diff, merge/approve). Token là **Personal Access
Token**.

1. <https://github.com/settings/tokens> → **Generate new token**.
   - Classic: scope `repo` (private) hoặc `public_repo`.
   - Fine-grained: cấp quyền **Contents** + **Pull requests** (Read/Write) trên đúng repo.
2. `GITHUB_DEFAULT_REPO` là repo mặc định khi user không nói rõ, dạng `owner/name`.
3. `GITHUB_USERNAME` là user dùng để push/approve (xem note về push auth bypass trong repo).

```dotenv
GITHUB_TOKEN=ghp_...
GITHUB_DEFAULT_REPO=gogox/da-api
GITHUB_USERNAME=TyronNhatAnh
```

---

## 3. Jira

Dùng **API token** của Atlassian Cloud (không phải password).

1. <https://id.atlassian.com/manage-profile/security/api-tokens> → **Create API token** → copy.
2. `JIRA_EMAIL` là email tài khoản Atlassian (chủ của token).
3. `JIRA_BASE_URL` là URL site, dạng `https://<org>.atlassian.net`.
4. `JIRA_DEFAULT_PROJECT` / `JIRA_BOARD_ID` là project key / board mặc định.

```dotenv
JIRA_BASE_URL=https://gogox.atlassian.net
JIRA_EMAIL=tyron.nguyen@gogox.com
JIRA_API_TOKEN=...
JIRA_DEFAULT_PROJECT=DAPro
JIRA_BOARD_ID=0
```

---

## 4. Notion

Dùng làm sink cho docs của revamp pipeline. Token rỗng → tool `notion_create_page` và
phần ghi Notion của pipeline tự tắt (pipeline báo thiếu config thay vì fail).

1. <https://www.notion.so/my-integrations> → **New integration** (internal) → copy
   **Internal Integration Secret** (`secret_...` / `ntn_...`).
2. Mở page cha mà bot sẽ nest doc vào → **⋯ → Connections → Add** integration vừa tạo
   (bắt buộc, nếu không API trả 404/permission).
3. `NOTION_PAGE_ID` là ID của page cha đó (32 ký tự hex trong URL).

```dotenv
NOTION_TOKEN=ntn_...
NOTION_PAGE_ID=36bf54d1149880bc966afc46ca018116
NOTION_VERSION=2022-06-28     # pin sẵn bởi integrations/notion.py, đừng đổi trừ khi biết rõ
```

---

## 5. Grafana (Loki logs)

Dùng để query log. Có **hai** tier riêng (staging + prod), mỗi tier một API key + base URL.

1. Grafana → **Administration → Service accounts** (hoặc **API keys** ở bản cũ) → tạo
   token role **Viewer** là đủ cho việc query log.
2. Tạo riêng cho staging và prod vì hai instance khác nhau.
3. `*_LOKI_UID` là UID của Loki datasource (thường là `loki`; xem ở
   **Connections → Data sources → Loki → URL/UID**).

```dotenv
GRAFANA_API_KEY_STAG=glsa_...
GRAFANA_STAG_BASE_URL=https://grafana-nonprod.gogo.tech
GRAFANA_STAG_LOKI_UID=loki

GRAFANA_API_KEY_PROD=glsa_...
GRAFANA_PROD_BASE_URL=https://grafana-kr.gogo.tech/
GRAFANA_PROD_LOKI_UID=loki
```

> Loki/Grafana hiển thị giờ UTC — khi đọc/ghi timestamp luôn quy đổi song song VN (UTC+7)
> và KST (UTC+9).

---

## 6. Service registry (`services.json`)

Bot biết các repo service qua một file JSON, đường dẫn khai báo ở `AGENTIC_SERVICES_JSON`
(mặc định `services.json` ở repo root), seed vào bảng `service_repos` lúc start.

```dotenv
AGENTIC_SERVICES_JSON=services.json
WORKSPACE_DIR=/Users/tyron/Projects/_workspaces   # nơi clone/worktree đáp xuống — phải set nếu dùng git action
WORKTREE_DIR=/Users/tyron/Projects/_worktrees
```

Schema mỗi entry:

```json
[
  {
    "name": "da-api",
    "repo_path": "/abs/path/local/clone",
    "github_repo": "gogox/da-api",
    "base_branch_template": "releases/DAPro-2.{sprint}",
    "jira_board_id": 0,
    "aliases": ["dapi"]
  }
]
```

---

## 7. Revamp tier (tùy chọn — da-api revamp)

Chỉ cần nếu dùng pipeline `revamp <scope>`. Tier này được bật theo **channel ID** chứ
không theo message text.

```dotenv
REVAMP_CHANNEL_ID=C...            # channel bật chế độ revamp/read-only. Trống = tắt khắp nơi.
REVAMP_LEGACY_REPO=/abs/path/da-api-legacy   # repo Ruby cũ, read-only, archaeologist đọc
REVAMP_TARGET_SERVICE=da-api-v2   # tên service (trong registry) repo đích
REVAMP_MODULE_CAP=40

# Read-only introspection schema hiện tại (schema.rb đã cũ) chạy qua API debug của
# ggx-kr-order-service (TEMPORARY, staging only — DB staging nằm sau VPN, host bot
# không tới được). Trống base URL/token = tool db_query tắt. Endpoint chỉ tồn tại
# khi service chạy non-prod — KHÔNG bao giờ trỏ vào prod.
ORDER_DEBUG_BASE_URL=https://<staging-host>
ORDER_DEBUG_ADMIN_TOKEN=          # admin accessToken (role AdminUser)
ORDER_DEBUG_ROW_CAP=200
ORDER_DEBUG_TIMEOUT_S=20
REVAMP_COMMON_MIGRATIONS_DIR=/abs/path/common-services/db/migrate
```

---

## 8. Chạy bằng Makefile

Dùng Makefile, đừng tự chế lệnh tương đương.

| Lệnh | Tác dụng |
| --- | --- |
| `make install` | `.venv` + editable install + copy `.env.example` → `.env` |
| `make run` | chạy foreground, Ctrl-C để dừng |
| `make debug` | foreground với `LOG_LEVEL=DEBUG` |
| `make start` | chạy nền; pid → `.agentic.pid`, log → `agentic.log` |
| `make stop` / `make restart` / `make status` | quản lý process nền |
| `make logs` | `tail -f agentic.log` |
| `make test` | `pytest -q` (hermetic, không gọi claude/Slack/GitHub thật) |
| `make db-show` | 20 dòng cuối bảng `runs` |
| `make db-stats` | cache_read ratio · cost/thread · tool fail rate |
| `make db-reset` | xóa `agentic.db` (tự tạo lại lần start sau) |
| `make clean` | dọn cache pyc/pytest/build |

Start xong sẽ thấy `⚡️ Bolt app started (Socket Mode)` trong log, rồi `@mention` bot
trong channel được allow.

> Chỉ chạy một instance. `make stop` trước khi `make debug` để tránh hai instance Socket
> Mode chia event.

---

## 9. Checklist tối thiểu để bot lên

1. `claude login` xong, `claude --version` ≥ min.
2. `make install`.
3. Điền `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` vào `.env`.
4. `make run` (hoặc `make debug`).
5. `@mention` bot trong một channel hợp lệ.

Các integration khác (GitHub/Jira/Notion/Grafana/revamp) bật thêm khi cần — token rỗng
thì tool đó tự tắt, bot vẫn chạy.
