"""Prompt loader.

The legacy `run_claude` subprocess runner + per-call usage tracker were removed
at the Phase 5 SDK cutover. `load_prompt` is the only survivor — it reads the
markdown system prompts that the SDK brain session + AgentDefinitions load.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
# The brain's cwd is the thread's service repo, so a relative `docs/…` in a prompt
# resolves nowhere. Prompts write `{DOCS}/x.md` and get this absolute path.
# Assumes the editable install layout (repo root = parents[3]).
DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"


def load_prompt(name: str) -> str:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return text.replace("{DOCS}", str(DOCS_DIR))
