import asyncio
import json
import logging
import re
import time
from pathlib import Path

from .agents import REGISTRY
from .agents.dev import run_dev as _run_dev_direct
from .agents.base import run_claude
from .brain import Action, BrainDecision, decide
from .brain import _format_messages as _format_thread_messages
from .config import settings
from .integrations import git as git_int
from .integrations import github, grafana, jira
from .integrations import ship as ship_int
from .integrations.result import ToolResult
from .store import (
    add_message,
    clear_pending_confirmation,
    get_pending_confirmation,
    get_thread,
    list_services,
    log_run,
    recent_messages,
    resolve_service_by_github_repo,
    save_pending_confirmation,
    touch_thread,
    update_thread_fields,
)

_AFFIRMATIVE = {
    "ok", "okay", "oke", "okie", "yes", "yeah", "yep",
    "ừ", "ừm", "uhm", "được", "đồng ý",
    "tiếp", "tiếp tục", "làm đi", "chốt", "ok b",
    "proceed", "go", "go ahead", "confirm",
}
_NEGATIVE = {
    "no", "không", "khong", "hủy", "huy", "stop", "cancel", "thôi",
    "thoi", "khỏi", "khoi", "abort",
}


def _normalize_reply(text: str) -> str:
    return text.strip().lower().rstrip(".!?。 ")


def _is_affirmative(text: str) -> bool:
    return _normalize_reply(text) in _AFFIRMATIVE


def _is_negative(text: str) -> bool:
    return _normalize_reply(text) in _NEGATIVE
from .summarizer import maybe_schedule as maybe_schedule_summary

log = logging.getLogger(__name__)


class DispatchResult:
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.errors: list[str] = []

    def add(self, text: str) -> None:
        if text:
            self.blocks.append(text)

    def render(self) -> str:
        body = "\n\n---\n\n".join(self.blocks) if self.blocks else "(no output)"
        if self.errors:
            body += "\n\n⚠️ " + "; ".join(self.errors)
        return body


_MAX_RETRIES = 2
_RETRY_BACKOFF_S = (0.5, 1.5)


def _footer(tool_count: int, elapsed_s: float) -> str:
    plural = "s" if tool_count != 1 else ""
    return f"_🛠️ {tool_count} tool{plural} · {elapsed_s:.1f}s_"


def _with_footer(reply: str, tool_count: int, t_start: float) -> str:
    if tool_count <= 0:
        return reply
    return f"{reply}\n\n{_footer(tool_count, time.time() - t_start)}"
_GITHUB_PR_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_REPO_SLUG_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")


def _truncate(text: str, limit: int, *, label: str = "input") -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit - 200
    tail = 100
    truncated = (
        text[:head]
        + f"\n…[{label} cắt bớt {len(text) - head - tail} ký tự]…\n"
        + text[-tail:]
    )
    return truncated, True


def _looks_like_local_repo_status_question(text: str) -> bool:
    lowered = text.lower()
    has_repo = "repo" in lowered or "local" in lowered or "working copy" in lowered
    asks_status = any(
        phrase in lowered
        for phrase in (
            "có chưa",
            "co chua",
            "có repo",
            "co repo",
            "có local",
            "co local",
            "chưa có repo",
            "chua co repo",
        )
    )
    return has_repo and asks_status


def _repo_from_text_or_history(text: str, messages: list[dict]) -> str | None:
    haystacks = [text] + [m.get("text") or "" for m in reversed(messages)]
    return _repo_from_haystacks(haystacks) or settings.github_default_repo


def _repo_from_haystacks(haystacks: list[str]) -> str | None:
    for item in haystacks:
        match = _GITHUB_PR_RE.search(item)
        if match:
            return match.group(1)
    for item in haystacks:
        match = _REPO_SLUG_RE.search(item)
        if match:
            return match.group(1)
    return None


def _repo_from_text_only(text: str) -> str | None:
    return _repo_from_haystacks([text])

# Reply gửi lên Slack nên rất ngắn: chỉ kết luận + link/keys + next step.
_REPLY_SAFE_LEN = 2500
_REPLY_SUMMARY_TIMEOUT = 45
_REPLY_SUMMARY_PROMPT = (
    "Bạn là trợ lý rút gọn phản hồi Slack bằng tiếng Việt.\n"
    "Viết LẠI phản hồi gốc thành tin nhắn Slack ngắn gọn, tối đa 2500 ký tự.\n"
    "Chỉ giữ: kết luận chính, Jira keys / PR links / URL quan trọng,\n"
    "và 1 dòng next step nếu có. Bỏ hết phần giải thích dài, code block lớn,\n"
    "log, diff. Văn bản thuần, không tiêu đề, không markdown fence."
)
_DEV_REPLY_SAFE_LEN = 1200
_DEV_REPLY_SUMMARY_PROMPT = (
    "Bạn là trợ lý rút gọn kết quả dev/fix cho Slack bằng tiếng Việt.\n"
    "Viết tối đa 6 dòng, dễ đọc. Giữ đúng dữ kiện, không thêm thông tin.\n"
    "Ưu tiên format:\n"
    "Status: <đã sửa / bị chặn>\n"
    "Changed: <file/ý chính nếu có>\n"
    "Verified: <lệnh test/build đã chạy hoặc chưa chạy>\n"
    "Next: <cần user làm gì nếu có>\n"
    "Bỏ bảng dài, phân tích dài, code block lớn."
)


async def _shrink_reply(text: str) -> str:
    if len(text) <= _REPLY_SAFE_LEN:
        return text
    try:
        summary = await run_claude(
            _REPLY_SUMMARY_PROMPT, text, timeout=_REPLY_SUMMARY_TIMEOUT,
            model=settings.agent_model,
        )
        summary = summary.strip()
    except Exception:
        log.exception("reply summarization failed; falling back to truncation")
        return text[: _REPLY_SAFE_LEN - 20] + "\n…(rút gọn)"
    if len(summary) > _REPLY_SAFE_LEN:
        summary = summary[: _REPLY_SAFE_LEN - 20] + "\n…(rút gọn)"
    return summary


async def _shrink_dev_reply(text: str) -> str:
    if len(text) <= _DEV_REPLY_SAFE_LEN and "|" not in text:
        return text
    try:
        summary = await run_claude(
            _DEV_REPLY_SUMMARY_PROMPT, text, timeout=_REPLY_SUMMARY_TIMEOUT,
            model=settings.agent_model,
        )
        return summary.strip()
    except Exception:
        log.exception("dev reply summarization failed; falling back to truncation")
        return text[: _DEV_REPLY_SAFE_LEN - 20] + "\n…(rút gọn)"


def _is_review_pr_request(text: str, action: Action) -> bool:
    if action.type != "github.get_pr_diff":
        return False
    lowered = text.lower()
    return "review" in lowered or "check pr" in lowered or "code review" in lowered


def _is_fix_pr_request(text: str, action: Action) -> bool:
    if action.type != "github.get_pr_diff":
        return False
    lowered = text.lower()
    return any(
        word in lowered
        for word in (
            "fix",
            "sửa",
            "sua",
            "apply patch",
            "patch",
            "implement",
        )
    )


def _is_read_action(action_type: str) -> bool:
    write_prefixes = (
        "github.create_",
        "github.comment_",
        "github.approve_",
        "github.merge_",
        "jira.create_",
        "jira.comment_",
        "jira.transition_",
        "git.",
        "ship.",
    )
    return not action_type.startswith(write_prefixes)


def _has_log_output(tool_outputs: list[tuple[str, str]]) -> bool:
    """True if any tool output is Loki log lines — those need grounding rules."""
    return any(at == "grafana.search_logs" for at, _ in tool_outputs)


async def _synthesize_action_reply(
    *,
    user_text: str,
    tool_outputs: list[tuple[str, str]],
    summary: str | None,
) -> str:
    system = (
        "Bạn là trợ lý Slack của Tyron. Trả lời tiếng Việt tự nhiên, ngắn gọn.\n"
        "Dựa vào tool outputs bên dưới để trả lời đúng câu user hỏi. "
        "Không bịa ngoài dữ liệu tool. Nếu dữ liệu là docs/specs/ticket detail, "
        "hãy tóm tắt phần liên quan thay vì dump raw toàn bộ. "
        "Giữ link/key quan trọng."
    )
    if _has_log_output(tool_outputs):
        # Log lines invite the model to fill in fields it never saw (user_id, hash,
        # device, ip) because an "incident report" format implies they exist. Force
        # field-level grounding so a truncated/absent value becomes UNKNOWN, not a guess.
        system += (
            "\n\nDữ liệu tool gồm log. Quy tắc BẮT BUỘC khi đọc log:\n"
            "- Chỉ nêu giá trị (id, order_id, user_id, ip, device, hash, status code...) "
            "nếu nó XUẤT HIỆN NGUYÊN VĂN trong dòng log. Tuyệt đối không suy diễn/điền cho đủ format.\n"
            "- Field không thấy trong log → ghi rõ `không có trong log` hoặc bỏ qua, KHÔNG đoán.\n"
            "- Dòng có `…[truncated]` là đã bị cắt — đừng bịa phần sau dấu cắt.\n"
            "- Tách rõ phần *quan sát từ log* (fact) và *giả thuyết* (suy luận của bạn)."
        )
    blocks = []
    if summary:
        blocks.append(f"## Thread summary\n{summary.strip()}")
    blocks.append(f"## User asked\n{user_text}")
    rendered_tools = "\n\n".join(
        f"### {action_type}\n{output}" for action_type, output in tool_outputs
    )
    blocks.append(f"## Tool outputs\n{rendered_tools}")
    return await run_claude(system, "\n\n".join(blocks), model=settings.agent_model)


async def _prepare_pr_workspace_context(repo: str, pr: str) -> tuple[str | None, str]:
    if repo == "unknown repo" or pr == "unknown PR":
        return None, ""
    try:
        workspace = await git_int.prepare_pr_review_workspace(repo, int(pr))
    except Exception as e:
        log.exception("prepare PR workspace failed")
        return None, f"Local workspace unavailable: {e}"
    if workspace.ok and isinstance(workspace.data, dict):
        cwd = workspace.data.get("repo_path")
        return cwd, (
            f"{workspace.data.get('message')}\n"
            f"Local repo cwd: `{cwd}`."
        )
    return None, (
        f"Local workspace unavailable: "
        f"{workspace.error_code or 'UNKNOWN'} - {workspace.user_message or ''}"
    )


async def _run_review_after_pr_diff(
    *,
    action: Action,
    diff: str,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
) -> tuple[str | None, str, str | None]:
    runner = REGISTRY.get("review")
    if not runner:
        return None, "error", "unknown agent `review`"

    repo = action.payload.get("repo") or settings.github_default_repo or "unknown repo"
    pr = action.payload.get("pr") or "unknown PR"
    cwd, local_context = await _prepare_pr_workspace_context(repo, str(pr))

    task = (
        f"Review PR #{pr} `{repo}` từ diff đã fetch. Trả findings theo template review. "
        "Nếu local workspace khả dụng, verify các nghi vấn bằng file thật trước khi kết luận."
    )
    context = diff if not local_context else f"{local_context}\n\n---\n{diff}"
    t0 = time.time()
    output: str | None = None
    status = "error"
    err: str | None = None
    try:
        output = await runner(task, context=context, cwd=cwd)
        status = "ok"
    except Exception as e:
        log.exception("agent review failed after fetching PR diff")
        err = str(e)
    log_run(
        agent="review",
        input_text=task,
        output=output,
        status=status,
        duration_ms=int((time.time() - t0) * 1000),
        thread_ts=thread_ts,
        channel=channel,
        user_id=user_id,
        error=err,
    )
    return output, status, err


async def _run_fix_after_pr_diff(
    *,
    action: Action,
    diff: str,
    user_text: str,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
) -> tuple[str | None, str, str | None]:
    runner = REGISTRY.get("dev")
    if not runner:
        return None, "error", "unknown agent `dev`"

    repo = action.payload.get("repo") or settings.github_default_repo or "unknown repo"
    pr = action.payload.get("pr") or "unknown PR"
    cwd, local_context = await _prepare_pr_workspace_context(repo, str(pr))
    if not cwd:
        return None, "error", local_context or "local workspace unavailable"

    task = (
        f"Fix request của user trong PR #{pr} `{repo}`. "
        f"User nói: {user_text}\n\n"
        "Bạn đang chạy trong local PR worktree. Sửa file trực tiếp trong workspace, "
        "rồi trả lời ngắn gọn: đã sửa gì, test/build đã chạy, còn gì cần user tự verify."
    )
    context = f"{local_context}\n\n---\n{diff}"
    t0 = time.time()
    output: str | None = None
    status = "error"
    err: str | None = None
    try:
        output = await runner(task, context=context, cwd=cwd, apply_changes=True)
        if output:
            output = await _shrink_dev_reply(output)
        status = "ok"
    except Exception as e:
        log.exception("agent dev failed after preparing PR workspace")
        err = str(e)
    log_run(
        agent="dev",
        input_text=task,
        output=output,
        status=status,
        duration_ms=int((time.time() - t0) * 1000),
        thread_ts=thread_ts,
        channel=channel,
        user_id=user_id,
        error=err,
    )
    return output, status, err


async def _invoke_integration(action: Action) -> ToolResult:
    if action.type.startswith("github."):
        return await github.execute_action(action.type, action.payload)
    if action.type.startswith("jira."):
        return await jira.execute_action(action.type, action.payload)
    if action.type.startswith("grafana."):
        return await grafana.execute_action(action.type, action.payload)
    if action.type.startswith("git."):
        return await git_int.execute_action(action.type, action.payload)
    if action.type.startswith("ship."):
        return await ship_int.execute_action(action.type, action.payload)
    return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action.type}`")


async def _run_action(action: Action) -> ToolResult:
    """Run an action with deterministic retry. Returns the final ToolResult.

    Write actions (create/comment/transition/git.*) are NEVER retried even on
    retryable errors — a timed-out POST may have succeeded server-side, and
    retrying would duplicate the Jira ticket / PR comment / branch.
    """
    can_retry = _is_read_action(action.type)
    last: ToolResult | None = None
    for attempt in range(_MAX_RETRIES + 1):
        result = await _invoke_integration(action)
        last = result
        if result.ok:
            return result
        if result.error_code == "NEEDS_CONFIRMATION":
            return result
        log.warning(
            "action %s failed (attempt %d): %s — %s",
            action.type, attempt + 1, result.error_code, result.user_message,
        )
        if not can_retry or not result.retryable or attempt >= _MAX_RETRIES:
            break
        await asyncio.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
    assert last is not None
    return last


_USER_FRIENDLY_ERROR_MESSAGES = {
    "UNKNOWN_ACTION": (
        "Mình chưa hỗ trợ thao tác này, hoặc bot đang chạy 2 instance lệch version. "
        "Ông thử restart bot (`make stop && make start`) rồi báo lại nha."
    ),
    "UNKNOWN": "Có lỗi không xác định khi gọi tool. Ông thử lại sau xíu nha.",
    "SERVER": "Service phía bên kia đang lỗi (5xx). Thử lại sau xíu.",
    "NETWORK": "Không kết nối được service. Có thể mạng đang lag.",
    "TIMEOUT": "Tool chạy quá lâu, đã timeout. Thử lại nha.",
    "RATE_LIMIT": "Bị rate-limit, đợi tí rồi thử lại.",
}


def _format_action_result(result: ToolResult) -> tuple[str, str, str | None]:
    if result.ok:
        return result.display(), "ok", None
    if result.error_code == "NEEDS_CONFIRMATION":
        return f"❓ {result.user_message}", "pending", "NEEDS_CONFIRMATION"
    soft_codes = {"AUTH", "CONFIG", "VALIDATION", "NOT_FOUND"}
    icon = "⚠️" if result.error_code in soft_codes else "❌"
    # Internal/transport codes have unhelpful raw messages — substitute a friendly
    # explanation and log the raw detail for debugging. AUTH/CONFIG/VALIDATION/
    # NOT_FOUND messages from tool authors are already user-facing.
    if result.error_code in _USER_FRIENDLY_ERROR_MESSAGES:
        friendly = _USER_FRIENDLY_ERROR_MESSAGES[result.error_code]
        log.warning(
            "tool error rendered as friendly message: code=%s raw=%r",
            result.error_code, result.user_message,
        )
        return f"{icon} {friendly}", "error", result.error_code
    return f"{icon} {result.user_message}", "error", result.error_code


async def _run_pending(
    pending: dict,
    *,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
) -> str:
    """Resume a previously-saved pending action after user confirmation."""
    action = Action(type=pending["action_type"], payload=dict(pending["payload"]))
    t0 = time.time()
    tool_result = await _run_action(action)
    display, status, error_code = _format_action_result(tool_result)
    log_run(
        agent=f"tool:{action.type}",
        input_text=str(action.payload),
        output=display,
        status=status,
        duration_ms=int((time.time() - t0) * 1000),
        thread_ts=thread_ts,
        channel=channel,
        user_id=user_id,
        error=error_code,
    )
    if thread_ts:
        add_message(thread_ts, "assistant", display)
        if status == "ok":
            update_thread_fields(thread_ts, last_agent=f"tool:{action.type}")
    return display


def _service_slug_from_registry_text(text: str) -> str | None:
    """Find a registered service mentioned by bare name/alias in text.

    Returns the service's github_repo slug (or canonical name) so downstream
    resolution and thread.repo persistence stay slug-shaped. Only matches keys
    containing a hyphen (canonical names, `ggx-kr-*` aliases, github repo tails)
    so short generic aliases like "order"/"user" don't hijack a free-text match.
    """
    haystack = text.lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_.-]*", haystack))
    if not tokens:
        return None
    for svc in list_services():
        keys = {(svc.get("name") or "").lower()}
        gh = (svc.get("github_repo") or "").strip().lower()
        if gh:
            keys.add(gh.rsplit("/", 1)[-1])
        try:
            keys.update(a.lower() for a in json.loads(svc.get("aliases") or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        keys = {k for k in keys if "-" in k}
        if keys & tokens:
            return gh or (svc.get("name") or None)
    return None


def _service_slug_from_registry(text: str, messages: list[dict]) -> str | None:
    current = _service_slug_from_registry_text(text)
    if current:
        return current
    for msg in reversed(messages):
        found = _service_slug_from_registry_text(msg.get("text") or "")
        if found:
            return found
    return None


def _dev_cwd_from_context(
    thread_row: dict | None,
    text: str = "",
    prior_messages: list[dict] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve (repo_slug, local_path) for the dev agent.

    Tries, in order:
      1. Explicit repo/PR slug in the current message — wins over everything so a
         concrete request never gets routed to a stale thread repo.
      2. thread.repo field (already persisted), only if no explicit slug.
      3. Bare service name/alias scanned from the registry, then repo/PR parsed
         from message history (or the default repo) — only when (1) found nothing.
    Returns (None, None) if no local mapping found.
    """
    candidates: list[str] = []
    explicit = _repo_from_text_only(text)
    if explicit:
        candidates.append(explicit)
    elif thread_row:
        slug = (thread_row.get("repo") or "").strip()
        if slug:
            candidates.append(slug)
    if not explicit:
        # Bare service name/alias (no owner prefix, e.g. "ggx-kr-da-api") — the
        # slug regex needs a "/", so scan the registry only after ruling out an
        # explicit repo in the current message. Never fall back to an old thread
        # repo when the user just gave a concrete repo/PR.
        scanned = _service_slug_from_registry(text, prior_messages or [])
        if scanned and scanned not in candidates:
            candidates.append(scanned)
        inferred = _repo_from_text_or_history(text, prior_messages or [])
        if inferred and inferred not in candidates:
            candidates.append(inferred)
    for slug in candidates:
        svc = resolve_service_by_github_repo(slug)
        if svc:
            path = svc.get("repo_path") or ""
            if path and Path(path).is_dir():
                return slug, path
    return None, None


_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _ticket_from_context(text: str, messages: list[dict]) -> str | None:
    m = _TICKET_RE.search(text)
    if m:
        return m.group(1)
    for msg in reversed(messages):
        m = _TICKET_RE.search(msg.get("text") or "")
        if m:
            return m.group(1)
    return None


async def _resolve_active_workspace(
    thread_row: dict | None, text: str, prior_messages: list[dict]
) -> dict | None:
    """Locate a prepared worktree for the ticket in play, resilient to whether
    `active_*` was persisted (older prepare runs didn't record it). Resolves the
    worktree by branch via git, so any on-disk path is found. Returns a dict with
    ticket/worktree/service/github_repo/base/feature_branch, or None."""
    ticket = ((thread_row or {}).get("active_ticket") or "").strip()
    if not ticket:
        ticket = _ticket_from_context(text, prior_messages) or ""
    if not ticket:
        return None
    slug = ((thread_row or {}).get("repo") or "").strip()
    slug = slug or _service_slug_from_registry(text, prior_messages) or ""
    svc = resolve_service_by_github_repo(slug) if slug else None
    if not svc:
        return None
    worktree = await git_int.resolve_existing_worktree(svc, ticket)
    if not worktree:
        return None
    base = ""
    try:
        base = await git_int._resolve_base_branch(svc)
    except Exception:
        log.warning("could not resolve base branch for active workspace")
    return {
        "ticket": ticket,
        "worktree": str(worktree),
        "service": svc["name"],
        "github_repo": (svc.get("github_repo") or "").strip(),
        "base": base,
        "feature_branch": f"feature/{ticket}",
    }


def _workspace_brain_hint(ws: dict) -> str:
    """Tell the brain a worktree is ready so it routes fix/PR work to dev."""
    return (
        "## Workspace đang mở\n"
        f"Thread này đã có worktree sẵn cho ticket `{ws['ticket']}` "
        f"(service `{ws['service']}`, branch `{ws['feature_branch']}`). "
        "Nếu user muốn fix/sửa/commit/push/tạo PR cho ticket này → trả 1 `dev` step "
        "(dev sẽ tự edit, commit, push, mở PR rồi báo link)."
    )


def _workspace_context_block(ws: dict) -> str:
    """Facts injected into the dev agent run (procedure lives in prompts/dev.md)."""
    lines = [
        "## Workspace (bạn đang đứng ở đây)",
        f"- Worktree (cwd của bạn): `{ws['worktree']}`",
        f"- Branch: `{ws['feature_branch']}`",
    ]
    if ws.get("github_repo"):
        lines.append(f"- Repo GitHub: `{ws['github_repo']}`")
    if ws.get("base"):
        lines.append(f"- Base branch để mở PR: `{ws['base']}`")
    lines.append(f"- Ticket: `{ws['ticket']}`")
    return "\n".join(lines)


def _dev_context_for_step(prior_output: str, prior_messages: list[dict]) -> str:
    if prior_output:
        context, _ = _truncate(
            prior_output, settings.max_context_chars, label="context"
        )
        return context
    thread_context = _format_thread_messages(prior_messages)
    if not thread_context:
        return ""
    context, _ = _truncate(
        thread_context, settings.dev_context_chars, label="dev context"
    )
    return context


async def handle_message(
    text: str,
    *,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
    thread_history: list[dict] | None = None,
) -> str:
    t_start = time.time()
    tool_count = 0
    text, input_truncated = _truncate(text, settings.max_input_chars, label="input")
    summary: str | None = None
    prior_messages: list[dict] = []
    pending: dict | None = None
    thread_row: dict | None = None
    if thread_ts:
        touch_thread(thread_ts, channel)
        thread_row = get_thread(thread_ts)
        summary = (thread_row or {}).get("summary")
        # Slack thread history (when available) covers non-mention user messages
        # that the DB never sees; fall back to DB if Slack fetch returned nothing.
        prior_messages = thread_history or recent_messages(thread_ts, limit=10)
        pending = get_pending_confirmation(thread_ts)
        add_message(thread_ts, "user", text)
    last_agent: str | None = None

    if _looks_like_local_repo_status_question(text):
        repo = _repo_from_text_or_history(text, prior_messages)
        action = Action(type="git.check_repo", payload={"repo": repo} if repo else {})
        t0 = time.time()
        tool_result = await _run_action(action)
        tool_count += 1
        display, status, error_code = _format_action_result(tool_result)
        log_run(
            agent="tool:git.check_repo",
            input_text=str(action.payload),
            output=display,
            status=status,
            duration_ms=int((time.time() - t0) * 1000),
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            error=error_code,
        )
        if thread_ts:
            add_message(thread_ts, "assistant", display)
            if status == "ok":
                fields: dict = {"last_agent": "tool:git.check_repo"}
                if repo:
                    fields["repo"] = repo
                update_thread_fields(thread_ts, **fields)
        return _with_footer(display, tool_count, t_start)

    # Resume / cancel a pending confirmation before invoking brain.
    if pending:
        if _is_affirmative(text):
            clear_pending_confirmation(thread_ts)
            pending_reply = await _run_pending(
                pending, thread_ts=thread_ts, channel=channel, user_id=user_id
            )
            tool_count += 1
            return _with_footer(pending_reply, tool_count, t_start)
        if _is_negative(text):
            clear_pending_confirmation(thread_ts)
            reply = "Ok đã hủy. Cho mình biết bạn muốn làm gì tiếp nhé."
            if thread_ts:
                add_message(thread_ts, "assistant", reply)
            return reply
        # Otherwise fall through to brain (treat as new request) but clear pending
        # so we don't leak it into the next turn.
        clear_pending_confirmation(thread_ts)

    # Resolve any worktree already prepared for the ticket in play. Used both to
    # nudge the brain (route fix/PR to dev) and to run dev inside that worktree.
    workspace = await _resolve_active_workspace(thread_row, text, prior_messages)
    if workspace and thread_ts:
        fields = {
            "active_ticket": workspace["ticket"],
            "active_worktree": workspace["worktree"],
        }
        if workspace.get("github_repo") and not (thread_row or {}).get("repo"):
            fields["repo"] = workspace["github_repo"]
        update_thread_fields(thread_ts, **fields)

    started = time.time()
    try:
        decision: BrainDecision = await decide(
            text, summary=summary, messages=prior_messages,
            workspace_hint=_workspace_brain_hint(workspace) if workspace else None,
        )
        tool_count += 1
    except Exception as e:
        tool_count += 1
        log_run(
            agent="brain",
            input_text=text,
            output=None,
            status="error",
            duration_ms=int((time.time() - started) * 1000),
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            error=str(e),
        )
        return _with_footer(f"❌ Brain failed: {e}", tool_count, t_start)

    log_run(
        agent="brain",
        input_text=text,
        output=decision.raw,
        status="ok",
        duration_ms=int((time.time() - started) * 1000),
        thread_ts=thread_ts,
        channel=channel,
        user_id=user_id,
    )

    if decision.need_clarification and decision.clarify_question:
        return _with_footer(
            f"❓ {decision.clarify_question}", tool_count, t_start
        )

    result = DispatchResult()
    if input_truncated:
        result.errors.append(
            f"input quá dài, đã cắt còn {settings.max_input_chars} ký tự"
        )
    if decision.reply and decision.reply.strip().lower() not in {"null", "none"}:
        result.add(decision.reply)

    steps = decision.steps[: settings.max_steps]
    if len(decision.steps) > settings.max_steps:
        result.errors.append(
            f"brain yêu cầu {len(decision.steps)} bước, chỉ chạy {settings.max_steps}"
        )
    actions = decision.actions[: settings.max_actions]
    if len(decision.actions) > settings.max_actions:
        result.errors.append(
            f"brain yêu cầu {len(decision.actions)} actions, chỉ chạy {settings.max_actions}"
        )

    prior_output = ""
    saw_review_output = False
    read_action_outputs: list[tuple[str, str]] = []
    for step in steps:
        runner = REGISTRY.get(step.agent)
        if not runner:
            result.errors.append(f"unknown agent `{step.agent}`")
            continue
        tool_count += 1
        t0 = time.time()
        output: str | None = None
        status = "error"
        err: str | None = None
        try:
            if step.agent == "dev":
                context_for_step = _dev_context_for_step(prior_output, prior_messages)
                # A worktree prepared earlier in this thread wins: the dev agent
                # must edit (and push/PR) inside it, not in the shared clone.
                if workspace and Path(workspace["worktree"]).is_dir():
                    dev_cwd = workspace["worktree"]
                    ws_ctx = _workspace_context_block(workspace)
                    context_for_step = (
                        f"{ws_ctx}\n\n---\n{context_for_step}"
                        if context_for_step else ws_ctx
                    )
                else:
                    dev_slug, dev_cwd = _dev_cwd_from_context(
                        thread_row if thread_ts else None,
                        text=text,
                        prior_messages=prior_messages,
                    )
                    if dev_slug and thread_ts and not (thread_row or {}).get("repo"):
                        update_thread_fields(thread_ts, repo=dev_slug)
                output = await _run_dev_direct(
                    step.task,
                    context=context_for_step,
                    cwd=dev_cwd,
                    apply_changes=bool(dev_cwd),
                )
            else:
                context_for_step, _ = _truncate(
                    prior_output, settings.max_context_chars, label="context"
                )
                output = await runner(step.task, context=context_for_step)
            status = "ok"
        except Exception as e:
            log.exception("agent %s failed", step.agent)
            err = str(e)
            result.errors.append(f"{step.agent}: {e}")
        log_run(
            agent=step.agent,
            input_text=step.task,
            output=output,
            status=status,
            duration_ms=int((time.time() - t0) * 1000),
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            error=err,
        )
        if output:
            result.add(f"**[{step.agent}]**\n{output}")
            prior_output = output
            if step.agent == "review":
                saw_review_output = True
        if status == "ok":
            last_agent = step.agent

    for action in actions:
        t0 = time.time()
        tool_result = await _run_action(action)
        tool_count += 1
        display, status, error_code = _format_action_result(tool_result)
        log_run(
            agent=f"tool:{action.type}",
            input_text=str(action.payload),
            output=display,
            status=status,
            duration_ms=int((time.time() - t0) * 1000),
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            error=error_code,
        )
        if (
            status == "ok"
            and action.type == "git.prepare_workspace"
            and thread_ts
            and isinstance(tool_result.data, dict)
        ):
            data = tool_result.data
            fields: dict = {}
            if data.get("ticket"):
                fields["active_ticket"] = data["ticket"]
            if data.get("worktree_path"):
                fields["active_worktree"] = data["worktree_path"]
            if data.get("github_repo"):
                fields["repo"] = data["github_repo"]
            if fields:
                update_thread_fields(thread_ts, **fields)
        if status == "ok" and _is_fix_pr_request(text, action):
            tool_count += 1
            fix_output, fix_status, fix_err = await _run_fix_after_pr_diff(
                action=action,
                diff=display,
                user_text=text,
                thread_ts=thread_ts,
                channel=channel,
                user_id=user_id,
            )
            if fix_status == "ok" and fix_output:
                result.add(f"**[dev]**\n{fix_output}")
                last_agent = "dev"
            else:
                result.add(display)
                result.errors.append(f"dev: {fix_err or 'failed'}")
                last_agent = f"tool:{action.type}"
        elif status == "ok" and _is_review_pr_request(text, action):
            tool_count += 1
            review_output, review_status, review_err = await _run_review_after_pr_diff(
                action=action,
                diff=display,
                thread_ts=thread_ts,
                channel=channel,
                user_id=user_id,
            )
            if review_status == "ok" and review_output:
                result.add(f"**[review]**\n{review_output}")
                last_agent = "review"
                saw_review_output = True
            else:
                result.add(display)
                result.errors.append(f"review: {review_err or 'failed'}")
                last_agent = f"tool:{action.type}"
        else:
            if status == "ok" and _is_read_action(action.type):
                read_action_outputs.append((action.type, display))
            else:
                result.add(display)
            if status == "ok":
                last_agent = f"tool:{action.type}"
        if status == "pending" and thread_ts:
            data = tool_result.data or {}
            save_pending_confirmation(
                thread_ts,
                action_type=data.get("action_type", action.type),
                payload=data.get("payload", action.payload),
                question=tool_result.user_message or "",
            )

    if read_action_outputs:
        try:
            synthesized = await _synthesize_action_reply(
                user_text=text,
                tool_outputs=read_action_outputs,
                summary=summary,
            )
            tool_count += 1
            result.add(synthesized.strip())
        except Exception as e:
            log.exception("action reply synthesis failed")
            for _, output in read_action_outputs:
                result.add(output)
            result.errors.append(f"synthesis: {e}")

    rendered = result.render()
    if not saw_review_output:
        if len(rendered) > _REPLY_SAFE_LEN:
            tool_count += 1
        rendered = await _shrink_reply(rendered)
    if thread_ts:
        add_message(thread_ts, "assistant", rendered)
        if last_agent:
            update_thread_fields(thread_ts, last_agent=last_agent)
        maybe_schedule_summary(thread_ts)
    return _with_footer(rendered, tool_count, t_start)
