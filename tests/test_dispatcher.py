import os
import tempfile

import pytest

os.environ.setdefault("AGENTIC_DB", tempfile.mktemp(suffix=".db"))

from agentic import dispatcher  # noqa: E402
from agentic.brain import BrainDecision, Step  # noqa: E402
from agentic.store import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    init_db()


async def _fake_decide(message, *, summary=None, messages=None):
    return BrainDecision(
        reply=None,
        steps=[Step(agent="ba", task="write user story for: " + message)],
        raw="(mocked)",
    )


async def _fake_ba(task, context=""):
    return f"STORY for task: {task}"


async def test_dispatcher_runs_single_agent(monkeypatch):
    monkeypatch.setattr(dispatcher, "decide", _fake_decide)
    monkeypatch.setitem(dispatcher.REGISTRY, "ba", _fake_ba)

    out = await dispatcher.handle_message(
        "login feature", thread_ts="t1", channel="C1", user_id="U1"
    )
    assert "STORY for task" in out
    assert "[ba]" in out


async def test_dispatcher_clarification(monkeypatch):
    async def clarify(msg, *, summary=None, messages=None):
        return BrainDecision(need_clarification=True, clarify_question="Which repo?")

    monkeypatch.setattr(dispatcher, "decide", clarify)
    out = await dispatcher.handle_message(
        "do stuff", thread_ts="t2", channel="C1", user_id="U1"
    )
    assert "Which repo?" in out
