import asyncio
import logging
import time

from .agents import REGISTRY
from .agents.base import run_claude
from .brain import Action, BrainDecision, decide
from .integrations import git as git_int
from .integrations import github, jira
from .integrations.result import ToolResult
from .store import (
    add_message,
    clear_pending_confirmation,
    get_pending_confirmation,
    get_thread,
    log_run,
    recent_messages,
    save_pending_confirmation,
    touch_thread,
    update_thread_fields,
)

_AFFIRMATIVE = {
    "ok", "okay", "oke", "okie", "yes", "y", "yeah", "yep",
    "ụ", "u", "uổ", "được", "đồng ý",
    "tiếp", "tiếp tục", "làm đi", "chốt", "ok b",
    "gửụg", "proceed", "go",
}
_NEGATIVE = {
    "no", "n", "không", "khong", "hủy", "huy", "stop", "cancel", "thôi",
    "thoi", "khỏi", "khoi",
}


def _is_affirmative(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in _AFFIRMATIVE


def _is_negative(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in _NEGATIVE
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

# Reply gửi lên Slack nên rất ngắn: chỉ kết luận + link/keys + next step.
_REPLY_SAFE_LEN = 800
_REPLY_SUMMARY_TIMEOUT = 45
_REPLY_SUMMARY_PROMPT = (
    "Bạn là trợ lý rút gọn phản hồi Slack bằng tiếng Việt.\n"
    "Viết LẠI phản hồi gốc thành tin nhắn Slack RẤT NGẮN, tối đa 800 ký tự.\n"
    "Chỉ giữ: kết luận chính, Jira keys / PR links / URL quan trọng,\n"
    "và 1 dòng next step nếu có. Bỏ hết phần giải thích dài, code block lớn,\n"
    "log, diff. Văn bản thuần, không tiêu đề, không markdown fence."
)


async def _shrink_reply(text: str) -> str:
    if len(text) <= _REPLY_SAFE_LEN:
        return text
    try:
        summary = await run_claude(
            _REPLY_SUMMARY_PROMPT, text, timeout=_REPLY_SUMMARY_TIMEOUT
        )
        summary = summary.strip()
    except Exception:
        log.exception("reply summarization failed; falling back to truncation")
        return text[: _REPLY_SAFE_LEN - 20] + "\n…(rút gọn)"
    if len(summary) > _REPLY_SAFE_LEN:
        summary = summary[: _REPLY_SAFE_LEN - 20] + "\n…(rút gọn)"
    return summary


async def _invoke_integration(action: Action) -> ToolResult:
    if action.type.startswith("github."):
        return await github.execute_action(action.type, action.payload)
    if action.type.startswith("jira."):
        return await jira.execute_action(action.type, action.payload)
    if action.type.startswith("git."):
        return await git_int.execute_action(action.type, action.payload)
    return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action.type}`")


async def _run_action(action: Action) -> ToolResult:
    """Run an action with deterministic retry. Returns the final ToolResult."""
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
        if not result.retryable or attempt >= _MAX_RETRIES:
            break
        await asyncio.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
    assert last is not None
    return last


def _format_action_result(result: ToolResult) -> tuple[str, str, str | None]:
    if result.ok:
        return result.display(), "ok", None
    if result.error_code == "NEEDS_CONFIRMATION":
        return f"❓ {result.user_message}", "pending", "NEEDS_CONFIRMATION"
    icon = "⚠️" if result.error_code in {"AUTH", "CONFIG", "VALIDATION", "NOT_FOUND"} else "❌"
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


async def handle_message(
    text: str,
    *,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
) -> str:
    summary: str | None = None
    prior_messages: list[dict] = []
    pending: dict | None = None
    if thread_ts:
        touch_thread(thread_ts, channel)
        thread_row = get_thread(thread_ts)
        summary = (thread_row or {}).get("summary")
        prior_messages = recent_messages(thread_ts, limit=10)
        pending = get_pending_confirmation(thread_ts)
        add_message(thread_ts, "user", text)
    last_agent: str | None = None

    # Resume / cancel a pending confirmation before invoking brain.
    if pending:
        if _is_affirmative(text):
            clear_pending_confirmation(thread_ts)
            return await _run_pending(
                pending, thread_ts=thread_ts, channel=channel, user_id=user_id
            )
        if _is_negative(text):
            clear_pending_confirmation(thread_ts)
            reply = "Ok đã hủy. Cho mình biết bạn muốn làm gì tiếp nhé."
            if thread_ts:
                add_message(thread_ts, "assistant", reply)
            return reply
        # Otherwise fall through to brain (treat as new request) but clear pending
        # so we don't leak it into the next turn.
        clear_pending_confirmation(thread_ts)

    started = time.time()
    try:
        decision: BrainDecision = await decide(
            text, summary=summary, messages=prior_messages
        )
    except Exception as e:
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
        return f"❌ Brain failed: {e}"

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
        return f"❓ {decision.clarify_question}"

    result = DispatchResult()
    if decision.reply and decision.reply.strip().lower() not in {"null", "none"}:
        result.add(decision.reply)

    prior_output = ""
    for step in decision.steps:
        runner = REGISTRY.get(step.agent)
        if not runner:
            result.errors.append(f"unknown agent `{step.agent}`")
            continue
        t0 = time.time()
        output: str | None = None
        status = "error"
        err: str | None = None
        try:
            output = await runner(step.task, context=prior_output)
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
        if status == "ok":
            last_agent = step.agent

    for action in decision.actions:
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
        result.add(display)
        if status == "ok":
            last_agent = f"tool:{action.type}"
        elif status == "pending" and thread_ts:
            data = tool_result.data or {}
            save_pending_confirmation(
                thread_ts,
                action_type=data.get("action_type", action.type),
                payload=data.get("payload", action.payload),
                question=tool_result.user_message or "",
            )

    rendered = result.render()
    rendered = await _shrink_reply(rendered)
    if thread_ts:
        add_message(thread_ts, "assistant", rendered)
        if last_agent:
            update_thread_fields(thread_ts, last_agent=last_agent)
        maybe_schedule_summary(thread_ts)
    return rendered
