import asyncio
import logging
import time

from .agents import REGISTRY
from .brain import Action, BrainDecision, decide
from .integrations import github, jira
from .integrations.result import ToolResult
from .store import (
    add_message,
    get_thread,
    log_run,
    recent_messages,
    touch_thread,
    update_thread_fields,
)
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


async def _invoke_integration(action: Action) -> ToolResult:
    if action.type.startswith("github."):
        return await github.execute_action(action.type, action.payload)
    if action.type.startswith("jira."):
        return await jira.execute_action(action.type, action.payload)
    return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action.type}`")


async def _run_action(action: Action) -> tuple[str, str, str | None]:
    """Run an action with deterministic retry. Returns (display_text, status, error_code)."""
    last: ToolResult | None = None
    for attempt in range(_MAX_RETRIES + 1):
        result = await _invoke_integration(action)
        last = result
        if result.ok:
            return result.display(), "ok", None
        log.warning(
            "action %s failed (attempt %d): %s — %s",
            action.type, attempt + 1, result.error_code, result.user_message,
        )
        if not result.retryable or attempt >= _MAX_RETRIES:
            break
        await asyncio.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
    assert last is not None
    icon = "⚠️" if last.error_code in {"AUTH", "CONFIG", "VALIDATION", "NOT_FOUND"} else "❌"
    return f"{icon} {last.user_message}", "error", last.error_code


async def handle_message(
    text: str,
    *,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
) -> str:
    summary: str | None = None
    prior_messages: list[dict] = []
    if thread_ts:
        touch_thread(thread_ts, channel)
        thread_row = get_thread(thread_ts)
        summary = (thread_row or {}).get("summary")
        prior_messages = recent_messages(thread_ts, limit=10)
        add_message(thread_ts, "user", text)
    last_agent: str | None = None

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
        text, status, error_code = await _run_action(action)
        log_run(
            agent=f"tool:{action.type}",
            input_text=str(action.payload),
            output=text,
            status=status,
            duration_ms=int((time.time() - t0) * 1000),
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            error=error_code,
        )
        result.add(text)
        if status == "ok":
            last_agent = f"tool:{action.type}"

    rendered = result.render()
    if thread_ts:
        add_message(thread_ts, "assistant", rendered)
        if last_agent:
            update_thread_fields(thread_ts, last_agent=last_agent)
        maybe_schedule_summary(thread_ts)
    return rendered
