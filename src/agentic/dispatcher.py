import logging
import time

from .agents import REGISTRY
from .agents.base import ClaudeRunError
from .brain import Action, BrainDecision, decide
from .integrations import github
from .store import log_run, recent_runs_for_thread, touch_thread

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


async def _run_action(action: Action) -> str:
    try:
        if action.type.startswith("github."):
            result = await github.execute_action(action.type, action.payload)
            return f"✅ `{action.type}` → {result}"
        return f"⚠️ unknown action `{action.type}`"
    except Exception as e:
        log.exception("action failed: %s", action.type)
        return f"❌ `{action.type}` failed: {e}"


async def handle_message(
    text: str,
    *,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
) -> str:
    if thread_ts:
        touch_thread(thread_ts, channel)
    history = recent_runs_for_thread(thread_ts) if thread_ts else []

    started = time.time()
    try:
        decision: BrainDecision = await decide(text, history)
    except ClaudeRunError as e:
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
    if decision.reply:
        result.add(decision.reply)

    prior_output = ""
    for step in decision.steps:
        runner = REGISTRY.get(step.agent)
        if not runner:
            result.errors.append(f"unknown agent `{step.agent}`")
            continue
        t0 = time.time()
        try:
            output = await runner(step.task, context=prior_output)
            status = "ok"
            err = None
        except ClaudeRunError as e:
            output = None
            status = "error"
            err = str(e)
            result.errors.append(f"{step.agent}: {e}")
        finally:
            log_run(
                agent=step.agent,
                input_text=step.task,
                output=output,
                status=status if "status" in locals() else "error",
                duration_ms=int((time.time() - t0) * 1000),
                thread_ts=thread_ts,
                channel=channel,
                user_id=user_id,
                error=err if "err" in locals() else None,
            )
        if output:
            result.add(f"**[{step.agent}]**\n{output}")
            prior_output = output

    for action in decision.actions:
        result.add(await _run_action(action))

    return result.render()
