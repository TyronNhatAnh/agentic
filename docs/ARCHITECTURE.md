# Kiến trúc — Brain session trên Claude Agent SDK

---

## 1. Mô hình tổng thể

Một câu Slack mention đi qua chuỗi sau. Python lo **plumbing + safety boundary**;
SDK lo **orchestration** (gọi tool, delegate sub-agent) — không có ReAct loop Python.

```
Slack app_mention
  │
  ▼
slack_handlers.py        allowlist channel · fetch thread (conversations.replies)
  │                      · post placeholder "Đang xử lý…" · submit Job
  ▼
worker.JobRunner         async queue, N workers · per-thread_ts busy set
  │                      (request trùng thread → reject, không queue)
  ▼
dispatcher.handle_message   resolve worktree đang mở cho ticket
  │                         · persist active_ticket/active_worktree/repo
  ▼
run_brain_session  ◄── ĐÂY là "lớp wrap" quanh SDK (KHÔNG phải claude -p)
  │
  ├─ ThreadSessionManager (client_pool)   1 ClaudeSDKClient / thread, idle-TTL evict
  ├─ make_brain_options_factory           dựng ClaudeAgentOptions per thread
  └─ client.query(user_msg)               SDK tự orchestrate tool_use + Task
       │
       └─ async for msg in client.receive_response():
              TextBlock     → buffer → stream vào Slack placeholder (debounce 1.5s)
              ToolUseBlock  → đếm (log per-tool nằm ở hooks)
              ResultMessage → terminal, lấy usage/cost/session_id
```

Tham chiếu code: [brain_session.py:180](../src/agentic/sdk/brain_session.py#L180)
(`run_brain_session`), [brain_session.py:90](../src/agentic/sdk/brain_session.py#L90)
(`factory`), [client_pool.py:45](../src/agentic/sdk/client_pool.py#L45)
(`get_or_create`).

---

## 2. Ba lớp bọc quanh SDK

Thứ "wrap" quanh `ClaudeSDKClient` không phải subprocess CLI, mà là 3 lớp Python:

### 2.1 Pooling — `ThreadSessionManager`
[client_pool.py](../src/agentic/sdk/client_pool.py)

- 1 `ClaudeSDKClient` / `thread_ts`, sống xuyên nhiều turn → giữ ấm prompt cache +
  conversation state.
- `OrderedDict` làm LRU: hết slot (`SDK_MAX_CONCURRENT_SESSIONS`, default 20) →
  evict LRU. Idle quá `SDK_SESSION_IDLE_TTL_S` (default 1800s) → `sweep_idle()` đóng
  (chạy mỗi 60s qua `_sdk_idle_sweeper`, [main.py:104](../src/agentic/main.py#L104)).
- `get_or_create` / `release` / `shutdown_all` đều dưới một `asyncio.Lock`.

### 2.2 Options factory — `make_brain_options_factory`
[brain_session.py:65](../src/agentic/sdk/brain_session.py#L65)

Dựng `ClaudeAgentOptions` **per thread** (vì channel cố định theo thread, prefix cache
vẫn ổn định):

| Field | Nguồn | Ghi chú |
|---|---|---|
| `system_prompt` | `policy.system_prompt` | prod=`brain_sdk`, revamp=`brain_revamp`; load 1 lần lúc startup |
| `mcp_servers` | `{"agentic": server}` | in-process `@tool`s ([mcp_tools.py](../src/agentic/sdk/mcp_tools.py)) |
| `model` | `settings.brain_model` | pin Opus; tunable qua `BRAIN_MODEL` |
| `permission_mode` | `"default"` | callback `can_use_tool` quyết định confirm |
| `max_turns` / `max_budget_usd` | `_loop_caps()` | per-turn circuit breaker (SDK-native); bỏ kwarg khi set 0 = unbounded ([:138](../src/agentic/sdk/brain_session.py#L138)) |
| `disallowed_tools` | `SESSION_DISALLOWED_TOOLS` | deny eval trước → strip khỏi context; chặn force-push/reset --hard/clean ở cả brain (`DEV_DISALLOWED_TOOLS` là alias cho sub-agent) |
| `can_use_tool` | `build_slack_permission_callback` | confirm ✅/❌ cho `CONFIRM_TOOLS` |
| `agents` | `build_subagents()` lọc theo policy | po/ba/review/dev qua native `Task` |
| `hooks` | `build_brain_hooks` | audit + per-tool `runs` rows |
| `resume` | `threads.sdk_session_id` | resume session sau restart |
| `session_store` | `session_store` | transcript append-only vào `session_entries` |
| `cwd` / `add_dirs` | `_session_dirs(row, policy)` | worktree hiện tại + writable roots |

### 2.3 Streaming bridge — trong `run_brain_session`
[brain_session.py:229](../src/agentic/sdk/brain_session.py#L229)

- Stream text của brain vào Slack placeholder, **debounce ~1.5s**
  (`_STREAM_EDIT_INTERVAL_S`) vì `chat.update` ~1/s/channel.
- Gặp Slack 429 → đọc `Retry-After`, đẩy lần edit kế tiếp ra xa (`_retry_after_seconds`).
- Mỗi edit streaming gắn suffix `⏳ đang xử lý…`; reply cuối cùng do **worker** render
  qua `job.reply` (không mang suffix) → suffix biến mất = tín hiệu "done".

---

## 3. Cache contract (quan trọng khi sửa)

System prompt + tool list **giữ nguyên** để prefix cache không churn.
`thread_history` + `workspace_hint` chỉ nhét vào **user message**, không bao giờ vào
system prompt — xem `_compose_user_message`
([brain_session.py:332](../src/agentic/sdk/brain_session.py#L332)).

```
## Bối cảnh thread (lịch sử Slack)
<transcript cap theo brain_history_budget_chars>

## Workspace hiện tại
<workspace_hint>

---
<user_text>
```

---

## 4. Observability

- `run_brain_session` log 1 dòng `brain` vào `runs` mang
  `cache_read / cache_creation / input / output tokens` + `cost_usd` từ
  `ResultMessage.usage`.
- Per-tool rows do **hooks** ghi (`PostToolUse` / `PostToolUseFailure`), không phải
  stream loop — xem [hooks.py](../src/agentic/sdk/hooks.py).
- `session_id` được persist về `threads.sdk_session_id` sau mỗi turn để resume.

---

## 5. Vì sao SDK, không phải `claude -p` — ưu/nhược + chi phí

### 5.1 Trade-off kiến trúc

| Tiêu chí | Claude Agent SDK (đang dùng) | `claude -p` thuần (Python orchestrate) |
|---|---|---|
| Orchestration | SDK tự lo tool loop + sub-agent (`Task`); Python chỉ là tool runtime | Python tự viết ReAct loop, parse stdout, gọi lại |
| Tool calling | Native `tool_use`, schema-validated theo `@tool` | Parse JSON-from-stdout, tự validate — dễ vỡ |
| Session/context | 1 client/thread long-lived, resume + auto-compaction | Stateless mỗi call, tự nhồi & cắt history |
| Prompt cache | Prefix giữ ấm cross-turn (đo được: ratio **0.806**) | Phải tự engineer mới giữ cache |
| Permission | `can_use_tool` callback match theo `tool_name`/`tool_input` | Parse text yes/no — đúng cái CLAUDE.md cấm |
| Hooks/observability | Lifecycle hooks → per-tool `runs` rows sẵn | Tự bọc timing/log quanh mỗi subprocess |
| Sub-agent | `AgentDefinition` share session prefix | Spawn process con, tự route/ghép |
| Streaming | Stream partial = progress indicator (1 `chat.update`) | Tự ghép stdout incremental |
| LoC Python | Mỏng (dispatcher ~290 LoC) | Dày: loop + parse + retry + state machine |
| Kiểm soát loop | Giới hạn theo abstraction SDK | Toàn quyền can thiệp giữa turn |
| Debug | "Hộp đen" hơn khi lỗi trong loop SDK | Lỗi nằm trong code mình → dễ trace |
| Auth/ToS | Đều `claude login` (OAuth seat) — **không khác**; multi-user vẫn vi phạm ToS dù đường nào | Như SDK |

**Kết**: workload ở đây là multi-turn · multi-tool · multi-thread · có sub-agent —
đúng profile SDK ăn đứt. SDK cấp miễn phí mọi thứ `claude -p` bắt tự xây (session,
cache, permission, hooks). `claude -p` chỉ đáng cân nhắc nếu cần kiểm soát loop ở mức
SDK không expose — hiện không có nhu cầu đó. Migration đã hoàn tất, không còn đường
`claude -p` (xem [MIGRATION_PLAN.md](../MIGRATION_PLAN.md)).

### 5.2 Chi phí — đo thật từ `runs` (`make db-stats`)

- **Cache read ratio = 0.806**: ~81% input token tính giá cache_read (~10% giá input).
  Prefix cache đang giữ ấm tốt → cost không phình theo độ dài thread. Mốc khỏe >0.7.
- **Cost driver là OUTPUT token + sub-agent, KHÔNG phải cache read.** Bằng chứng từ
  top-cost threads:

  | thread | cost | cache_in | out_tok | turns | đọc ra |
  |---|---|---|---|---|---|
  | 1780240914 | $25.59 | 1.4M | 64k | 37 | đắt: 64k output + 3 sub-agent ~200s/lần |
  | 1780228753 | $16.69 | 670k | 109k | 60 | output cao nhất |
  | 1780127594 | $4.93 | **2.5M** | 23k | 59 | cache nhiều **nhất** nhưng rẻ nhất → output thấp |

  Thread cache 2.5M lại rẻ nhất, thread output 64–109k mới đắt → **tiền nằm ở chữ
  model sinh ra + thời gian sub-agent**, không ở cache.
- **Đòn giảm cost**: ép sub-agent (dev/review) trả gọn — đừng dán nguyên file vào
  output; soi số lần spawn sub-agent/turn. Giảm cache read gần như vô nghĩa.

### 5.3 Tool fail — bản chất là gọi sai input, không phải hạ tầng

`tool fail rate ≈ 6.9%`. Phân loại fail (verify từ input thật trong `runs`):

- `github_get_pr/_diff`: brain **đoán slug repo** (`order-service`, `gogox/...`) thay vì
  resolve qua `list_services` → VALIDATION/NOT_FOUND.
- `github_search`: dùng qualifier không hỗ trợ (`head:`) → HTTP 422.
- `grafana_search_logs`: phần lớn là **`[AUTH]` token chết** (glsa_), đã fix ở commit
  292c10a (SA basic-auth) — **không phải lỗi schema**.

→ Fix đã áp (commit 037e101): nhồi hint format `owner/name` + trỏ `list_services` vào
field `repo`, và liệt kê qualifier hợp lệ cho `github_search`. Đúng tinh thần "improve
the context/schema contract", không hard-code phrase. Hiệu quả cần theo dõi
`make db-stats` sau vài turn để xác nhận (HYPOTHESIS).

---