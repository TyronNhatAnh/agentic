"""Prompt loader.

The legacy `run_claude` subprocess runner + per-call usage tracker were removed
at the Phase 5 SDK cutover. `load_prompt` is the only survivor — it reads the
markdown system prompts that the SDK brain session + AgentDefinitions load.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
# The brain's cwd is the thread's tier repo, not this repo, so a relative doc
# path in a prompt resolves somewhere that doesn't exist (5 dead Reads of
# `docs/GOGOX_ARCHITECTURE.md` in 3 days). Prompts write `{DOCS}/x.md` and get
# the absolute path — same trick `_ARCH_DOC`/`_DB_TABLES_DOC` use for tool
# descriptions. Host-constant, so the session prefix cache stays warm.
DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"


def load_prompt(name: str) -> str:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return text.replace("{DOCS}", str(DOCS_DIR))
