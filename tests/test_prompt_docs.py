import json
import re
from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "agentic" / "prompts"


def _json_code_blocks(text: str) -> list[str]:
    matches = re.findall(r"```(?:json)?\n(.*?)\n```", text, re.DOTALL)
    return [block.strip() for block in matches if block.strip().startswith("{")]


def test_brain_prompt_json_examples_are_valid():
    text = (PROMPTS_DIR / "brain.md").read_text(encoding="utf-8")

    blocks = _json_code_blocks(text)

    assert blocks, "brain prompt should include JSON examples"
    assert '"user story login google"\n→ steps:' not in text
    assert '"review diff sau"\n→ steps:' not in text
    assert '"fix bug redis timeout"\n→ steps:' not in text
    assert '"chào"\n→ reply trực tiếp' not in text

    for block in blocks:
        data = json.loads(block)
        assert isinstance(data, dict)
        assert "reply" in data
        assert "need_clarification" in data
        assert "clarify_question" in data
        assert "steps" in data
        assert "actions" in data