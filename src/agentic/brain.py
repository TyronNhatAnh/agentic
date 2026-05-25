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
    actions = [Action(type=a["type"], payload=a.get("payload") or {}) for a in data.get("actions") or []]
    return BrainDecision(
        reply=data.get("reply"),
        need_clarification=bool(data.get("need_clarification")),
        clarify_question=data.get("clarify_question"),
        steps=steps,
        actions=actions,
        raw=text,
    )


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    lines = []
    for h in history[-10:]:
        agent = h.get("agent", "?")
        inp = (h.get("input") or "").strip().replace("\n", " ")[:200]
        out = (h.get("output") or "").strip().replace("\n", " ")[:200]
        lines.append(f"[{agent}] in: {inp} | out: {out}")
    return "\n".join(lines)


async def decide(user_message: str, history: list[dict] | None = None) -> BrainDecision:
    system = load_prompt("brain")
    hist = _format_history(history or [])
    user = user_message if not hist else f"Recent thread:\n{hist}\n\nNew message:\n{user_message}"
    raw = await run_claude(system, user)
    try:
        return parse_decision(raw)
    except Exception as e:
        log.warning("brain parse failed: %s; falling back to raw reply", e)
        return BrainDecision(reply=raw, raw=raw)
