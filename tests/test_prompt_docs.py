"""Prompt sanity checks after the Phase 5 cutover.

brain.md (legacy JSON-output prompt) is gone; brain_sdk.md is the single brain
prompt and must NOT carry a JSON output spec — the SDK injects tool schemas and
the brain emits native tool_use blocks."""

from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "agentic" / "prompts"


def test_sdk_prompts_present():
    for name in ("brain_sdk", "dev", "review", "ba", "po"):
        assert (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def test_legacy_brain_prompt_removed():
    assert not (PROMPTS_DIR / "brain.md").exists()


def test_brain_sdk_prompt_has_no_json_output_spec():
    text = (PROMPTS_DIR / "brain_sdk.md").read_text(encoding="utf-8")
    # The old prompt forced a `{"reply": ..., "steps": [...], "actions": [...]}`
    # envelope; the SDK path must not reintroduce it.
    assert '"need_clarification"' not in text
    assert '"steps":' not in text
