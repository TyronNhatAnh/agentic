# Kiến trúc — Brain session trên Claude Agent SDK

> **Điểm cốt lõi (đọc trước):** Không có lớp nào "wrap SDK client vào `claude -p`".
> Migration đã hoàn tất — đường `claude -p` / ReAct loop Python đã bị **gỡ bỏ**.
> Runtime thật là `ClaudeSDKClient` gọi `client.query()` trực tiếp.
> `subprocess` chỉ còn sót ở 2 chỗ không liên quan brain: check `claude --version`
> lúc startup ([main.py:40](../src/agentic/main.py#L40)) và chạy lệnh `git`
> ([integrations/git.py:28](../src/agentic/integrations/git.py#L28)).

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

Tham chiếu code: [brain_session.py:160](../src/agentic/sdk/brain_session.py#L160)
(`run_brain_session`), [brain_session.py:85](../src/agentic/sdk/brain_session.py#L85)
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
  (chạy mỗi phút từ [main.py:101](../src/agentic/main.py#L101)).
- `get_or_create` / `release` / `shutdown_all` đều dưới một `asyncio.Lock`.

### 2.2 Options factory — `make_brain_options_factory`
[brain_session.py:60](../src/agentic/sdk/brain_session.py#L60)

Dựng `ClaudeAgentOptions` **per thread** (vì channel cố định theo thread, prefix cache
vẫn ổn định):

| Field | Nguồn | Ghi chú |
|---|---|---|
| `system_prompt` | `policy.system_prompt` | prod=`brain_sdk`, revamp=`brain_revamp`; load 1 lần lúc startup |
| `mcp_servers` | `{"agentic": server}` | in-process `@tool`s ([mcp_tools.py](../src/agentic/sdk/mcp_tools.py)) |
| `model` | `settings.brain_model` | pin Opus; tunable qua `BRAIN_MODEL` |
| `disallowed_tools` | `DEV_DISALLOWED_TOOLS` | chặn force-push/reset --hard/clean ở cả brain |
| `can_use_tool` | `build_slack_permission_callback` | confirm ✅/❌ cho `CONFIRM_TOOLS` |
| `agents` | `build_subagents()` lọc theo policy | po/ba/review/dev qua native `Task` |
| `hooks` | `build_brain_hooks` | audit + per-tool `runs` rows |
| `resume` | `threads.sdk_session_id` | resume session sau restart |
| `cwd` / `add_dirs` | `_session_dirs(row, policy)` | worktree hiện tại + writable roots |

### 2.3 Streaming bridge — trong `run_brain_session`
[brain_session.py:193](../src/agentic/sdk/brain_session.py#L193)

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
([brain_session.py:273](../src/agentic/sdk/brain_session.py#L273)).

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

## 5. Cái KHÔNG tồn tại (chống hiểu nhầm)

- ❌ Không có `claude -p` subprocess path.
- ❌ Không có Python ReAct loop / JSON-from-stdout parsing.
- ❌ Không có cờ `AGENTIC_USE_SDK` (migration xong, không còn nhánh điều kiện).
- ❌ Không có progress-message loop riêng — partial text stream **chính là** progress.
- ❌ Không có bảng `pending_confirmations` — confirm state là `asyncio.Future`
  in-memory.

Lịch sử migration: [MIGRATION_PLAN.md](../MIGRATION_PLAN.md).
