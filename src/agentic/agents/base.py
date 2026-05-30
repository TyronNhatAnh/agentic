"""Prompt loader.

The legacy `run_claude` subprocess runner + per-call usage tracker were removed
at the Phase 5 SDK cutover. `load_prompt` is the only survivor — it reads the
markdown system prompts that the SDK brain session + AgentDefinitions load.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
