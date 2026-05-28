import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .agents.base import load_prompt, run_claude
from .config import settings

log = logging.getLogger(__name__)


@dataclass
class Step:
    agent: str
    task: str


@dataclass
class Action:
    type: str
    payload: dict[str, Any]


@dataclass
class BrainDecision:
    reply: str | None = None
    need_clarification: bool = False
    clarify_question: str | None = None
    steps: list[Step] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    raw: str = ""


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Strip code fences if model wrapped output.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in brain output: {text[:200]}")
    return json.loads(match.group(0))


def parse_decision(text: str) -> BrainDecision:
    data = _extract_json(text)
    steps = [Step(agent=s["agent"], task=s["task"]) for s in data.get("steps") or []]
    actions = [
        Action(type=a.get("type") or a["tool"], payload=a.get("payload") or {})
        for a in data.get("actions") or []
    ]
    return BrainDecision(
        reply=data.get("reply"),
        need_clarification=bool(data.get("need_clarification")),
        clarify_question=data.get("clarify_question"),
        steps=steps,
        actions=actions,
        raw=text,
    )


def _format_messages(messages: list[dict]) -> str:
    if not messages:
        return ""
    lines = []
    budget = settings.brain_history_budget_chars
    msg_cap = settings.brain_history_msg_cap_chars
    for m in messages:
        role = m.get("role", "?")
        text = (m.get("text") or "").strip()
        line = f"{role}: {text}"
        if len(line) > msg_cap:
            line = line[:msg_cap] + f"\n…[message cắt bớt {len(line) - msg_cap} ký tự]"
        if sum(len(existing) for existing in lines) + len(line) > budget:
            remaining = max(0, budget - sum(len(existing) for existing in lines))
            if remaining > 200:
                lines.append(line[:remaining] + "\n…[history cắt bớt]")
            break
        lines.append(line)
    return "\n".join(lines)


async def decide(
    user_message: str,
    *,
    summary: str | None = None,
    messages: list[dict] | None = None,
    workspace_hint: str | None = None,
    tool_results: list[str] | None = None,
) -> BrainDecision:
    system = load_prompt("brain")
    parts: list[str] = []
    if summary:
        parts.append(f"## Tóm tắt hội thoại trước đó\n{summary.strip()}")
    if workspace_hint:
        parts.append(workspace_hint.strip())
    recent = _format_messages(messages or [])
    if recent:
        parts.append(f"## Tin nhắn gần đây\n{recent}")
    parts.append(f"## Tin nhắn mới của user\n{user_message}")
    if tool_results:
        per_cap = settings.max_context_chars
        total_cap = per_cap * 4
        capped = []
        for r in tool_results:
            if len(r) > per_cap:
                r = r[:per_cap] + f"\n…[cắt bớt {len(r) - per_cap} ký tự]…"
            capped.append(r)
        formatted = "\n\n---\n".join(capped)
        if len(formatted) > total_cap:
            formatted = formatted[:total_cap] + f"\n…[tool results cắt bớt]…"
        parts.append(f"## Kết quả công cụ vừa chạy\n{formatted}")
    user = "\n\n".join(parts)
    raw = await run_claude(system, user, model=settings.brain_model)
    try:
        return parse_decision(raw)
    except Exception as e:
        log.warning("brain JSON parse failed (attempt 1): %s; raw_head=%r", e, raw[:200])
        # Retry: wrap the prose output back into a JSON reply.
        retry_user = (
            user
            + "\n\n---\nOutput của bạn vừa rồi không phải JSON hợp lệ.\n"
            "Hãy wrap nội dung đó vào đúng format JSON:\n"
            '{"reply": "<nội dung đó>", "need_clarification": false, "clarify_question": null, "steps": [], "actions": []}'
        )
        raw2 = ""
        try:
            raw2 = await run_claude(system, retry_user, model=settings.brain_model)
            return parse_decision(raw2)
        except Exception as e2:
            log.error("brain JSON parse failed (attempt 2): %s; raw_head=%r", e2, raw2[:500])
        fallback = (
            "⚠️ Brain trả response không phải JSON hợp lệ; bot không chạy step/action nào.\n"
            "Raw model output:\n" + raw
        )
        return BrainDecision(reply=fallback, raw=raw)
