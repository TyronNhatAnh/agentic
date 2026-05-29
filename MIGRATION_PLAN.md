# Migration Plan — `claude -p` subprocess → `claude-agent-sdk`

**Mục tiêu:** chuyển bot sang kiến trúc agent production-grade: per-thread session sống, function calling thật, prompt cache cao, sub-agent thấy nhau, permission flow native.

---

## 🚪 Brief cho session mới

Bạn đang nhận handover một migration đang chạy. File này là single source of truth: §3 chỉ phase đang ở đâu, §2 mô tả kiến trúc đích, §6 nói file nào liên quan cho từng phase, §12 chốt contract code (signature, schema, cơ chế) cho phần đã thiết kế sẵn.

Cách làm việc kỳ vọng:

- Bắt đầu turn bằng cách đọc plan + §6 cho phase đang làm + §12.X tương ứng. Đọc thêm file nếu thực sự cần để hoàn thành task — đọc rộng tốn token và làm session drift.
- Quyết định kiến trúc đã chốt ở §1–§5, §12. Nếu có trade-off mới đáng đổi hướng, ghi §8 (Decisions log) với evidence rồi tiếp tục — đừng tự sửa §1–§7.
- Hỏi user khi gặp ambiguity không giải quyết được bằng plan + source. Hỏi cụ thể, có file:line. Đừng hỏi để confirm thứ §12 đã viết rõ; đừng hỏi để hedge sự không chắc của mình (đọc thêm thay vì hỏi).
- Cập nhật checkbox §3 ngay sau mỗi step để session sau biết tình trạng. Append §8 khi có decision mới.
- Mỗi session gói gọn ~1 phase. Khi thấy bắt đầu lặp lại hoặc lan man — đề xuất user `/clear` mở session mới (xem §7).

Prompt mẫu khi `/clear`:

> Đọc `MIGRATION_PLAN.md`. Báo phase đang ở đâu, đã đọc gì, có ambiguity nào trong §12 không. Đợi xác nhận trước khi code.

---

## 1. Diagnosis (đã chốt — không bàn lại)

10 pain points của bot hiện tại, xếp theo impact:

| # | Vấn đề | Root cause |
|---|---|---|
| 1 | Bot "không nhớ" giữa các turn | History nhồi tay vào user_prompt, mỗi turn spawn `claude -p` mới ([brain.py:98-119](src/agentic/brain.py#L98-L119)) |
| 2 | Cold start mỗi step | 5 subprocess/request |
| 3 | Cache hit thấp | History đổi mỗi turn → `cache_creation` thay vì `cache_read` |
| 4 | Dev không edit được code | `permission_mode=None` khi `dev_cwd=None` ([dev.py:47](src/agentic/agents/dev.py#L47)) |
| 5 | Brain output fragile | JSON parse từ stdout, không function calling ([brain.py:51-65](src/agentic/brain.py#L51-L65)) |
| 6 | Sub-agent mù lẫn nhau | Context chuyền qua string `context=...` |
| 7 | Confirmation tay | `pending_confirmations` + regex yes/no ([dispatcher.py:32-53](src/agentic/dispatcher.py#L32-L53)) |
| 8 | Phrase matching overfit | [dispatcher.py:109-152](src/agentic/dispatcher.py#L109-L152) |
| 9 | History truncate cứng | `brain_history_budget_chars` ([brain.py:72](src/agentic/brain.py#L72)) |
| 10 | Multi-step không stream | User chờ 30-60s, chỉ "đang xử lý..." |

**Quyết định kỹ thuật:** kiến trúc hiện tại là *"shell script wrap LLM"*. Target là **LLM driver, Python = tool runtime + safety boundary**.

---

## 2. Target architecture (đã chốt)

```
Slack (slack_handlers.py — giữ nguyên)
  ↓ Job(thread_ts, text)
ThreadSessionManager (dict[thread_ts → ClaudeSDKClient], TTL idle 30')
  ↓
ClaudeSDKClient (sống suốt thread, resume qua SessionStore SQLite)
  options = ClaudeAgentOptions(
    system_prompt=<brain.md gọn>,
    agents={dev, review, po, ba} = AgentDefinition,
    mcp_servers={agentic=SdkMcpServer(tools=[github,jira,git,grafana,ship])},
    permission_mode="default",
    can_use_tool=<slack_permission_callback>,
    hooks={PreToolUse, PostToolUse, PreCompact},
    resume=<session_id từ threads table>,
  )
  ↓ Claude tự ReAct native, không Python orchestrate
```

**Đổi đời quan trọng:**

- **Brain biến mất khỏi Python.** Brain = Claude với system prompt + tools, output `tool_use` blocks. Python chỉ làm tool runtime.
- **Sub-agent = `AgentDefinition`** trong cùng session, gọi qua `Task` tool native.
- **Confirmation = `can_use_tool` callback** ([SDK types.py:234-273](file:///tmp/casdk/unpacked/claude_agent_sdk/types.py#L234)). Cơ chế: **`asyncio.Future` in-memory per pending request** (KHÔNG dùng `pending_confirmations` table — table chỉ phục vụ legacy path tới khi cutover Phase 5). Callback post Slack ❓ → `await future` → user reply trigger Future qua hook trong [slack_handlers.py](src/agentic/slack_handlers.py) → `PermissionResultAllow/Deny`. Khi session SDK chạy, request **block trong async context của chính nó**, không cần persist sang turn sau. Chi tiết signature ở §12.
- **History native** qua session resume. **Bỏ** `summary` tự sinh + `recent_messages` nhồi user prompt.
- **MCP server in-process** cho integrations — tool schema typed, không hallucinate field.

---

## 3. Phased plan + status checklist

Cập nhật checkbox khi xong. **Mỗi phase merge được độc lập, có rollback flag `AGENTIC_USE_SDK`.**

### Phase 0 — Foundation (0.5 ngày) ✅ DONE 2026-05-29
- [x] `claude-agent-sdk>=0.2.87,<0.3.0` vào [pyproject.toml](pyproject.toml)
- [x] Tạo [src/agentic/sdk/](src/agentic/sdk/) module skeleton
- [x] [sdk/client_pool.py](src/agentic/sdk/client_pool.py) — `ThreadSessionManager` (OrderedDict LRU + idle TTL sweep + capacity-bounded)
- [x] [sdk/session_store.py](src/agentic/sdk/session_store.py) — `SqliteSessionStore` round-trips entries via `threads.sdk_state_blob`; subagent subpath stubbed for Phase 1
- [x] [sdk/mcp_tools.py](src/agentic/sdk/mcp_tools.py) — sample `github_get_pr` `@tool` + `build_agentic_mcp_server()` (Phase 2 expand)
- [x] Schema migration: `sdk_session_id`, `sdk_state_blob` ở [store.py:_THREAD_ADDED_COLUMNS](src/agentic/store.py)
- [x] Env flag `AGENTIC_USE_SDK` + `MIN_CLAUDE_VERSION` + `SDK_SESSION_IDLE_TTL_S` + `SDK_MAX_CONCURRENT_SESSIONS` ở [config.py](src/agentic/config.py)
- [x] Startup version check ở [main.py:_check_claude_version](src/agentic/main.py) — fail fast nếu < `MIN_CLAUDE_VERSION`
- [x] Smoke test [tests/test_sdk_smoke.py](tests/test_sdk_smoke.py) — 6 tests: imports / mcp build / store round-trip / subagent skip / session caching / LRU eviction. **Không** spawn `claude` thực (hermetic).

**Pass criteria:** ✅ `pytest -q` 39 passed; ✅ smoke 6/6 pass; ✅ `_check_claude_version()` confirm `2.1.133`.

### Phase 1 — Dev agent (2-3 ngày, value cao nhất)
- [ ] `sdk/permission.py` — `PendingPermissions` (dict[req_id → asyncio.Future]) + `slack_permission_callback(...)` factory. Xem §12.A.
- [ ] `sdk/dev_agent.py` — `run_dev_sdk(...)` với signature đầy đủ §12.B. Lấy client từ `ThreadSessionManager`, gắn `slack_permission_callback`, stream messages, return final reply text.
- [ ] `sdk/dev_options.py` — `build_dev_options(thread_ts, cwd, permission_cb)` → `ClaudeAgentOptions`. Tools/permissions theo §12.C.
- [ ] Wire vào [slack_handlers.py](src/agentic/slack_handlers.py): khi user reply mà thread có pending permission → resolve Future thay vì submit Job mới. Xem §12.D.
- [ ] Plug vào [dispatcher.py:914-940](src/agentic/dispatcher.py#L914-L940) gated bằng `settings.use_sdk`. Xem §12.E.
- [ ] Eval 5 ticket: edit + commit + push + open PR end-to-end **không cần user pin service**

**Pass criteria:**
- Dev edit file được dù `dev_cwd` ban đầu = None
- `cache_read / (cache_read + cache_creation)` của dev step > 60%
- Latency p50 dev step < 70% baseline (đo từ `ResultMessage.usage`)

### Phase 2 — Brain → function calling (3-4 ngày, đụng nhiều)
- [ ] `sdk/mcp_tools.py` — convert TẤT CẢ integration verbs thành `@tool` với `input_schema` JSON typed. Mapping 1-1 với [github.py](src/agentic/integrations/github.py), [jira.py](src/agentic/integrations/jira.py), [git.py](src/agentic/integrations/git.py), [grafana.py](src/agentic/integrations/grafana.py), [ship.py](src/agentic/integrations/ship.py).
- [ ] `sdk/brain_session.py` thay [brain.py](src/agentic/brain.py): 1 SDKClient/thread, system_prompt = `brain.md` ĐÃ rút (bỏ tool catalog prose, SDK auto inject)
- [ ] Rút gọn [prompts/brain.md](src/agentic/prompts/brain.md) — bỏ phần liệt kê tool, bỏ phrase mapping. Chỉ giữ: vai trò, principle, output style.
- [ ] [dispatcher.py](src/agentic/dispatcher.py) `handle_message()` còn ~200 dòng: Slack I/O, session lookup, stream output. Bỏ ReAct loop tay ([:836-940](src/agentic/dispatcher.py#L836-L940)), bỏ `tool_results` concat.
- [ ] Xóa `_looks_like_local_repo_status_question` ([:136-152](src/agentic/dispatcher.py#L136-L152)), `_BANNED_REPLY_PRONOUNS` ([:109-118](src/agentic/dispatcher.py#L109-L118))

**Pass criteria:**
- Bot route đúng cho 10 biến thể phrasing test set
- Không còn `KeyError`/`ValueError` từ malformed brain JSON
- Latency p50 toàn request < 60% baseline

### Phase 3 — Sub-agents → AgentDefinition (1-2 ngày)
- [ ] Convert PO/BA/Review → `AgentDefinition` entries trong `ClaudeAgentOptions(agents=...)`
- [ ] Mỗi agent: description, prompt (từ [prompts/](src/agentic/prompts/)), tools subset, model override nếu cần
- [ ] Brain delegate qua native `Task` tool — bỏ Python `REGISTRY` dispatch ([agents/__init__.py](src/agentic/agents/__init__.py))
- [ ] Xóa `agents/{po,ba,review}.py` (giữ `dev.py` vì worktree-aware logic riêng)

**Pass criteria:** review agent đọc PR diff + cross-check file trong worktree **trong cùng 1 turn**, không cần Python ghép context.

### Phase 4 — Hooks + streaming + observability (2 ngày)
- [ ] `HookMatcher(PreToolUse)` → audit log, redact secrets, insert `runs` table
- [ ] `HookMatcher(PostToolUse)` → update_thread_fields, schedule summary (nếu giữ)
- [ ] `HookMatcher(PreCompact)` → cảnh báo session sắp full
- [ ] Slack streaming: edit placeholder mỗi 1.5s debounce (chú ý rate limit `chat.update`)
- [ ] Bỏ [summarizer.py](src/agentic/summarizer.py) — dùng compaction native
- [ ] Dashboard query: cost/thread, cache_read ratio, tool fail rate

**Pass criteria:** user thấy progress realtime; `runs` table không mất event so với baseline.

### Phase 5 — Cleanup + harden (1-2 ngày)
- [ ] Bỏ flag `AGENTIC_USE_SDK` (full cutover)
- [ ] Xóa code legacy: [brain.py](src/agentic/brain.py), `pending_confirmations` schema cũ, [agents/base.py:run_claude](src/agentic/agents/base.py)
- [ ] Update [CLAUDE.md](CLAUDE.md) — kiến trúc mới
- [ ] Load test 10 thread × 5 turn — đo cost, latency, memory pool

**Pass criteria:** `dispatcher.py` xuống ≤ 700 LoC (từ 1086), feature parity.

---

## 4. Design choices — tận dụng tối đa Claude

Plan này dựa trên Claude Agent SDK, khai thác các đặc trưng sau:

1. **Prompt caching prefix-stable.** System prompt + tool schema + agent defs cố định ⇒ `cache_read` cao. Thread state đi qua session messages, không nhồi system prompt.
2. **Native multi-turn session.** `ClaudeSDKClient` per thread, resume bằng `session_id` lưu ở `threads.sdk_session_id`. Bỏ ghép `summary + recent_messages` tay vào user prompt.
3. **Function calling thật.** Tools qua `SdkMcpServer` + `@tool` typed schema. Brain output `tool_use` blocks, SDK validate input — không JSON parse stdout.
4. **Auto-compaction.** Claude Code tự compact khi gần đầy context; điều chỉnh qua `PreCompactHook` nếu cần. Bỏ summarizer Python tự sinh.
5. **AgentDefinition cho sub-agents.** PO/BA/Dev/Review share session state, brain delegate qua `Task` tool native.
6. **Hooks.** `PreToolUse` audit/redact, `PostToolUse` ghi `runs` table, `PreCompact` cảnh báo.
7. **Permission mode** chọn theo agent: `default` cho brain, `acceptEdits` cho dev session trong worktree.
8. **Thinking adaptive.** `ThinkingConfigAdaptive` cho task phức tạp — Claude tự budget thinking tokens.
9. **Streaming.** Stream `AssistantMessage` blocks → Slack progress realtime, không chờ full response.
10. **Observability.** `usage` từ `ResultMessage` per request; `SDKSessionInfo` cho debug session state.

### Pitfall đã thấy ở bot cũ — đừng tái phạm

Dispatcher hiện tại đoán intent qua phrase matching cứng ([dispatcher.py:32-53](src/agentic/dispatcher.py#L32-L53), [109-152](src/agentic/dispatcher.py#L109-L152)). Vấn đề: user nói khác chút là sai route, lockout language, dễ false positive. Plan này thay bằng các pattern không cần đoán user:

- **Permission flow** — match SDK tool descriptor (`tool_name`, `tool_input.command`), typed + ngôn ngữ-agnostic. Xem §12.A.
- **User confirm** — Slack block kit button với `action_id` tự define. Không parse text reply. Xem §12.D.
- **Intent routing** — brain SDK với function calling, Claude tự decide từ tool schema + thread session context.

Nguyên tắc: nếu thấy mình sắp viết `if "ok" in text` hay regex match user message → dừng, đọc lại §2 + §12.X của phase đang làm. Gần như chắc chắn có cách typed/structured thay thế.

---

## 5. Risks + rollback

| Risk | Mitigation |
|---|---|
| SDK breaking change | Pin `>=0.2.87,<0.3.0`; integration test catch |
| Session subprocess leak | `ThreadSessionManager` TTL 30' idle; atexit cleanup; cap N=20 concurrent |
| Permission callback deadlock | Timeout 5' → auto-Deny |
| Cost tăng không kiểm soát | Phase 0: `WORKER_CONCURRENCY=1`, cost cap per thread; rollback flag `AGENTIC_USE_SDK=false` |
| Thread cũ mất context khi cutover | Flag chạy song song phase 1-4; thread cũ tiếp với legacy, thread mới SDK |
| Tool schema mismatch | Phase 2 chạy shadow 1 tuần — log diff SDK call vs brain JSON cũ |
| `claude` binary < 2.0.0 | Phase 0 check startup, fail fast |

**Rollback bất kỳ phase nào:** set `AGENTIC_USE_SDK=false` trong `.env`, `make restart`. Code legacy chưa xóa tới Phase 5.

---

## 6. Reference map — đọc gì cho phase nào

| Phase | Cần đọc | KHÔNG cần đọc |
|---|---|---|
| 0 | [config.py](src/agentic/config.py), [store.py](src/agentic/store.py) §_THREAD_ADDED_COLUMNS, SDK `__init__.py` exports | dispatcher.py, brain.py |
| 1 | [agents/dev.py](src/agentic/agents/dev.py), [agents/base.py:run_claude](src/agentic/agents/base.py), [dispatcher.py:568-700](src/agentic/dispatcher.py#L568-L700) (`_dev_cwd_from_context`, `_run_dev_direct` callsite) | brain.py, integrations/* |
| 2 | [brain.py](src/agentic/brain.py), [prompts/brain.md](src/agentic/prompts/brain.md), [integrations/*.py](src/agentic/integrations/) signatures (chỉ verb list), [dispatcher.py:830-1000](src/agentic/dispatcher.py#L830) | slack_handlers, store schema chi tiết |
| 3 | [agents/{po,ba,review}.py](src/agentic/agents/), [prompts/{po,ba,review}.md](src/agentic/prompts/), `agents/__init__.py` REGISTRY | dispatcher (đã done phase 2) |
| 4 | SDK types.py §HookMatcher (~line 274-475), [summarizer.py](src/agentic/summarizer.py), Slack rate limit docs | source legacy đã thay |
| 5 | Diff tổng kết, [CLAUDE.md](CLAUDE.md) | — |

**SDK source local copy:** `/tmp/casdk/unpacked/claude_agent_sdk/` — grep ở đó, không cần re-download.

---

## 7. 🚨 Drift signals — khi nào nhắc user mở session mới

Đây là tín hiệu session hiện tại đã "ngu đi". Khi Claude thấy ≥ 1 trong các điều dưới, **CHỦ ĐỘNG NHẮC**: *"Session bắt đầu drift (lý do: X). Anh `/clear` rồi mở session mới và bảo nó đọc `MIGRATION_PLAN.md`."*

**Tín hiệu drift:**

1. **Đề xuất đi ngược plan** — Claude propose architecture khác §2, hoặc đụng vào anti-pattern §4. → drift.
2. **Quên phase đang làm** — không nhớ checkbox §3 nào đã tick, hỏi lại "đang ở phase nào". → drift.
3. **Phải đọc > 10 file để làm 1 step** — vi phạm §6 reference map. Có thể plan thiếu, có thể session mất focus.
4. **Lặp lại câu hỏi đã trả lời trước** — vd hỏi lại "có dùng API key không?" sau khi đã chốt §2.
5. **Output đề xuất prose dài > 500 từ** thay vì chỉnh code. Session đang lan man.
6. **Token usage hiển thị > 70%** context window (Claude Code status). Compaction sắp tới — restart cleaner.
7. **Sai file path/line** so với §6, không catch ra. Memory đã trôi.
8. **Code đề xuất không reference SDK class names** đã có ở §2 (`ClaudeSDKClient`, `AgentDefinition`, `SdkMcpServer`, `HookMatcher`). Đang quên SDK.

**Mỗi session mới chỉ nên cover 1 phase**, tối đa 2. Phase 2 đụng nhiều — nên 1 session riêng. Phase 0+1 có thể chung 1 session.

**Cost-aware:** session mới đọc plan này (~6k tokens) + reference file phase đó (~5-15k tokens). Tổng base ~20k. Còn ~150k cho code work. Nếu session đã consume > 100k mà chưa xong phase → drift, restart.

---

## 8. Decisions log — ghi quyết định mới khi đụng

Khi session phát hiện vấn đề plan không cover, **không tự sửa §1-§7**. Append vào đây với format:

```
### YYYY-MM-DD — Phase X — <topic>
Vấn đề: ...
Quyết định: ...
File ảnh hưởng: ...
```

**Đã chốt:**

- **2026-05-29** — Phase 0 — Auth: dùng `claude login` subscription, không API key. Verified từ SDK source `SubprocessCLITransport` ([client.py:25](file:///tmp/casdk/unpacked/claude_agent_sdk/_internal/client.py#L25)).
- **2026-05-29** — Phase 1 — Dev sandbox bug root cause: [dev.py:47](src/agentic/agents/dev.py#L47) chỉ set permission khi `apply_changes=True`, `apply_changes=bool(dev_cwd)` ở [dispatcher.py:939](src/agentic/dispatcher.py#L939). Fix qua SDK (Phase 1), không hot-fix `claude -p`.
- **2026-05-29** — Phase 2 — Brain prompt 237 dòng có overfit tool catalog prose + phrase mapping. Rút gọn khi convert sang SDK, để SDK auto inject tool schema.

---

## 9. Success metrics

**Đo bằng `runs` table + log `claude usage` SAU Phase 0 (không có baseline tin cậy vì log hiện tại không có "claude usage" entry — bot ít traffic + missing scope `groups:history` chặn request ở channel private).**

Dùng absolute target, không delta:

| Metric | Target Phase 5 |
|---|---|
| `cache_read / (cache_read + cache_creation)` của brain calls | > 75% |
| `cache_read / total` của dev session calls | > 60% |
| Latency p50 / request | < 12s |
| Latency p95 / request | < 30s |
| Cost USD / thread (10 turn) | < $0.30 |
| Tool call malformed rate (KeyError/ValueError parse) | 0 |
| Dev edit success khi chưa pin service | > 80% (eval set 5 ticket) |
| `dispatcher.py` LoC | ≤ 700 (từ 1086) |

**Eval set 5 ticket** để chấm Phase 1-3: thiết kế khi vào Phase 1 (5 scenario dev/review/PO thật từ Slack history).

---

## 10. Open questions cần anh confirm trước Phase 0

- [x] `claude --version` trên host bot ≥ 2.0.0? — **Confirmed 2026-05-29: `2.1.133 (Claude Code)`** ✓
- [ ] Thread đang active lúc deploy: reset session hay resume từ summary cũ?
- [ ] Slack quota `chat.update` channel hiện tại — Phase 4 streaming tăng 5-10×/turn, có rủi ro không?
- [ ] Giữ `pending_confirmations` cho path legacy đến hết Phase 5, hay xóa sớm ở Phase 2?

## 12. Implementation contracts — đọc trước khi hỏi user

Mỗi phase phải define signature + cơ chế **trước** khi code. Section này chốt cho phase đang/sắp làm. Câu hỏi nào còn lại sau khi đọc § này mới được hỏi user.

### 12.A — Permission flow (Phase 1+)

```python
# src/agentic/sdk/permission.py
from asyncio import Future
from claude_agent_sdk import (
    ToolPermissionContext, PermissionResult,
    PermissionResultAllow, PermissionResultDeny,
)

class PendingPermissions:
    """In-memory map: req_id → Future. Resolved by Slack reply handler.
    Per-process, không cần persist — SDK session block trong async context."""
    def __init__(self) -> None:
        self._futures: dict[str, Future[bool]] = {}
    def create(self, req_id: str) -> Future[bool]: ...
    def resolve(self, req_id: str, allow: bool) -> bool:
        """Return True nếu req_id có pending Future; False nếu không (→ message
        đó là user request thường, fall through brain)."""
    def pop(self, req_id: str) -> Future[bool] | None: ...

# Tools cần confirm (whitelist explicit, KHÔNG regex):
CONFIRM_TOOLS: set[str] = {
    "github_merge_pr", "github_approve_pr",
    # Bash với pattern "git push:*" — match qua tool_input["command"]
}
CONFIRM_BASH_PATTERNS: tuple[str, ...] = ("git push",)

def build_slack_permission_callback(
    *, pending: PendingPermissions, slack_client, channel_id: str,
    thread_ts: str, timeout_s: int = 300,
):
    """Factory trả về CanUseTool async callback. Khi Claude muốn dùng tool,
    callback kiểm tra whitelist; nếu cần confirm → post Slack ❓ + tạo Future →
    await với timeout → trả PermissionResult."""
    async def cb(tool_name, tool_input, ctx: ToolPermissionContext) -> PermissionResult:
        if not _needs_confirm(tool_name, tool_input):
            return PermissionResultAllow(behavior="allow", updated_input=tool_input)
        req_id = f"{thread_ts}:{ctx.tool_use_id}"
        fut = pending.create(req_id)
        await slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=f"❓ Cho phép `{tool_name}` chạy? Reply `ok` / `huỷ`. (req {req_id[-6:]})",
        )
        try:
            allow = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            pending.pop(req_id)
            return PermissionResultDeny(behavior="deny", message="timeout 5'")
        return (
            PermissionResultAllow(behavior="allow", updated_input=tool_input)
            if allow else
            PermissionResultDeny(behavior="deny", message="user huỷ")
        )
    return cb
```

**Rule:** match tool **theo tool_name + tool_input.command**, KHÔNG regex tin nhắn user. Anti-pattern §4 cấm phrase matching áp dụng cho intent của user; ở đây ta match SDK tool descriptor — typed, deterministic, OK.

### 12.B — `run_dev_sdk` signature

```python
# src/agentic/sdk/dev_agent.py
async def run_dev_sdk(
    task: str,
    *,
    thread_ts: str,
    channel_id: str,
    slack_client,                # AsyncWebClient từ slack_bolt
    placeholder_ts: str,         # message Slack để edit khi stream
    cwd: str | None,             # worktree khi có, else None → workspace_dir
    context: str = "",           # prior step output (Phase 3 mới tận dụng đầy đủ)
    pool: ThreadSessionManager,  # injected từ caller
    pending: PendingPermissions, # injected
) -> str:
    """Stream từ ClaudeSDKClient của thread, edit placeholder mỗi ~1.5s
    (debounce). Trả về final reply text. Permission tool → §12.A callback."""
```

Caller (dispatcher) chịu trách nhiệm tạo `pool` + `pending` singleton ở startup ([main.py](src/agentic/main.py)) và inject xuống. Không global.

### 12.C — Dev options

```python
# src/agentic/sdk/dev_options.py
ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)",
    "Bash(git fetch:*)", "Bash(git rev-parse:*)",
    "Bash(git branch:*)", "Bash(git checkout:*)",
    "Bash(gh pr create:*)", "Bash(gh pr view:*)",
    "Bash(gh pr list:*)", "Bash(gh pr comment:*)",
]
# Disallowed copy y nguyên từ agents/dev.py:23-31 (force push, reset --hard, etc.)

def build_dev_options(*, cwd, system_prompt, permission_cb, session_store):
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="acceptEdits",   # always — bỏ điều kiện apply_changes
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DEV_DISALLOWED,
        add_dirs=[cwd] if cwd else [settings.workspace_dir],
        cwd=cwd or settings.workspace_dir,
        can_use_tool=permission_cb,
        session_store=session_store,
        model=settings.dev_model,
        include_partial_messages=True,   # cho streaming
    )
```

### 12.D — Slack reply → resolve permission Future

**KHÔNG parse text user.** Dùng Slack interactive button (block_actions). Đây là idiomatic Slack + bỏ hoàn toàn phrase matching:

```python
# Trong build_slack_permission_callback (§12.A) — thay chat_postMessage text bằng blocks:
await slack_client.chat_postMessage(
    channel=channel_id, thread_ts=thread_ts,
    text=f"❓ Cho phép `{tool_name}` chạy?",
    blocks=[
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"❓ Cho phép tool *{tool_name}* chạy?\n```{json.dumps(tool_input, ensure_ascii=False)[:500]}```"}},
        {"type": "actions", "block_id": f"perm:{req_id}", "elements": [
            {"type": "button", "action_id": "perm_allow",
             "text": {"type": "plain_text", "text": "✅ Cho phép"},
             "style": "primary", "value": req_id},
            {"type": "button", "action_id": "perm_deny",
             "text": {"type": "plain_text", "text": "❌ Huỷ"},
             "style": "danger", "value": req_id},
        ]},
    ],
)
```

Slack handler ([slack_handlers.py](src/agentic/slack_handlers.py)) đăng ký 2 action handler:

```python
@app.action("perm_allow")
async def _on_allow(ack, body, ...):
    await ack()
    pending.resolve(body["actions"][0]["value"], allow=True)

@app.action("perm_deny")
async def _on_deny(ack, body, ...):
    await ack()
    pending.resolve(body["actions"][0]["value"], allow=False)
```

**Edge case:** user reply text thay vì bấm button → plan này KHÔNG bịa cách parse. Text reply đó đi thẳng vào brain (path bình thường), pending Future vẫn chờ button hoặc timeout 5'. Nếu sau khi user reply mà Future timeout → callback trả `Deny` với message "không bấm button, timeout"; user thấy → bấm hoặc nói lại.

**Lý do KHÔNG dùng text parsing:** anti-pattern §4 cấm phrase matching intent. Loophole "đang chờ yes/no nên closed set OK" mà tôi từng viết là sai — chính là pattern cũ của [dispatcher.py:32-53](src/agentic/dispatcher.py#L32-L53) đang phải xoá. Button là Slack-native, deterministic, không cần đoán ngôn ngữ.

### 12.E — Dispatcher gate

```python
# dispatcher.py:914-940 — chỉ branch theo flag, KHÔNG xoá legacy
if settings.use_sdk:
    output = await run_dev_sdk(
        step.task,
        thread_ts=thread_ts, channel_id=channel,
        slack_client=_slack_client_singleton(),
        placeholder_ts=_progress_message_ts(progress),
        cwd=effective_cwd, context=context_for_step,
        pool=_pool_singleton(), pending=_pending_singleton(),
    )
else:
    output = await _run_dev_direct(
        step.task, context=context_for_step,
        cwd=effective_cwd, apply_changes=bool(dev_cwd),
    )
```

Singleton pattern: `_pool_singleton()` / `_pending_singleton()` khởi tạo lazy ở [main.py](src/agentic/main.py) sau `init_db()`, inject vào dispatcher qua module-level. Đơn giản — khi session_id của ThreadSessionManager đã đủ cô lập per-thread.

---

## 11. Side issues phát hiện trong lúc plan (tách thành PR riêng, không block migration)

- **Bot missing scope `groups:history`** — Slack API trả `missing_scope` khi `conversations.replies` ở channel private. Bot fail request **trước khi tới brain**, không log gì ngoài Slack response. Fix: thêm scope `groups:history` (và `mpim:history` nếu cần channel DM nhóm) vào Slack App config, reinstall workspace. Evidence: [agentic.log](agentic.log) entry 2026-05-27 11:06:07 trả `'ok': False, 'error': 'missing_scope', 'needed': 'groups:history'`.
