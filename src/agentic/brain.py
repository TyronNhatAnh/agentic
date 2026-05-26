import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .agents.base import load_prompt, run_claude

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
    budget = 12000
    for m in messages:
        role = m.get("role", "?")
        text = (m.get("text") or "").strip()
        line = f"{role}: {text}"
        if len(line) > 2500:
            line = line[:2400] + f"\n…[message cắt bớt {len(line) - 2400} ký tự]"
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
) -> BrainDecision:
    system = load_prompt("brain")
    parts: list[str] = []
    if summary:
        parts.append(f"## Tóm tắt hội thoại trước đó\n{summary.strip()}")
    recent = _format_messages(messages or [])
    if recent:
        parts.append(f"## Tin nhắn gần đây\n{recent}")
    parts.append(f"## Tin nhắn mới của user\n{user_message}")
    user = "\n\n".join(parts)
    raw = await run_claude(system, user)
    try:
        return parse_decision(raw)
    except Exception as e:
        log.error(
            "brain JSON parse failed: %s; raw_head=%r",
            e,
            raw[:500],
        )
        fallback = (
            "⚠️ Brain trả response không phải JSON hợp lệ; bot không chạy step/action nào.\n"
            "Raw model output:\n" + raw
        )
        return BrainDecision(reply=fallback, raw=raw)
