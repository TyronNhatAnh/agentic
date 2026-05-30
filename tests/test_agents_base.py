"""The agents package now holds only the prompt loader (run_claude was removed
at the Phase 5 SDK cutover)."""

import pytest

from agentic.agents import base


def test_load_prompt_reads_markdown():
    text = base.load_prompt("brain_sdk")
    assert text.strip()


def test_load_prompt_missing_raises():
    with pytest.raises(FileNotFoundError):
        base.load_prompt("does_not_exist_prompt")
