"""End-to-end coverage of one brain turn's Slack-visible behavior: what the
placeholder shows while the SDK is streaming, stalling, or timing out, and what
the footer reports. Drives the real `run_brain_session` against a scripted SDK
client + a fake Slack, so the stream loop, heartbeat, debounce and 429 backoff
all run for real. Motivated by the 2026-08-07 stall (344s of silence between
`client.query()` and the first token, placeholder frozen the whole time).
"""

import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from agentic.config import settings
from agentic.sdk import PendingPermissions
from agentic.sdk import brain_session as bs


class FakeSlack:
    """Records every placeholder edit; can be told to rate-limit."""

    def __init__(self, rate_limit_after: int | None = None, retry_after: int = 5):
        self.updates: list[str] = []
        self._rate_limit_after = rate_limit_after
        self._retry_after = retry_after

    async def chat_update(self, *, channel, ts, text, blocks):
        body = blocks[0]["text"]
        if (
            self._rate_limit_after is not None
            and len(self.updates) >= self._rate_limit_after
        ):
            raise _RateLimited(self._retry_after)
        self.updates.append(body)


class _RateLimited(Exception):
    """Shaped like slack_sdk's SlackApiError enough for _retry_after_seconds."""

    def __init__(self, retry_after: int):
        super().__init__("ratelimited")
        self.response = {"headers": {"Retry-After": str(retry_after)}}


class ScriptedClient:
    """Streams a script of (delay_s, message) pairs. `None` message = pure stall."""

    def __init__(self, script):
        self._script = script
        self.queries: list[str] = []

    async def query(self, prompt, *_a, **_k):
        self.queries.append(prompt)

    async def receive_response(self):
        for delay, msg in self._script:
            if delay:
                await asyncio.sleep(delay)
            if msg is not None:
                yield msg


class FakePool:
    def __init__(self, client):
        self._client = client
        self.released: list[str] = []

    async def get_or_create(self, thread_ts):
        return self._client

    async def release(self, thread_ts):
        self.released.append(thread_ts)


def _assistant(*blocks):
    return AssistantMessage(content=list(blocks), model="claude-opus-5")


def _delta(text, parent_tool_use_id=None):
    """A token-level text delta as the SDK wraps it when include_partial_messages
    is on (`event` is the raw Anthropic stream event)."""
    return StreamEvent(
        uuid="u1",
        session_id="sess-e2e",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
        parent_tool_use_id=parent_tool_use_id,
    )


def _result(text="final answer", **kw):
    return ResultMessage(
        subtype="success",
        duration_ms=kw.pop("duration_ms", 1200),
        duration_api_ms=1100,
        is_error=False,
        num_turns=kw.pop("num_turns", 1),
        session_id=kw.pop("session_id", "sess-e2e"),
        stop_reason="end_turn",
        total_cost_usd=kw.pop("cost", 0.05),
        usage=kw.pop("usage", {"input_tokens": 5, "output_tokens": 200}),
        result=text,
    )


async def _run(script, slack, thread_ts, **kw):
    pool = FakePool(ScriptedClient(script))
    result = await bs.run_brain_session(
        user_text=kw.pop("user_text", "hi"),
        thread_ts=thread_ts,
        channel_id="C1",
        slack_client=slack,
        placeholder_ts="1700000000.1",
        thread_history=kw.pop("thread_history", []),
        workspace_hint=None,
        pool=pool,
        pending=PendingPermissions(),
    )
    return result, pool


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    """Real intervals are 1.5s / 20s — compressed so the suite stays fast."""
    from agentic.store import init_db

    init_db()  # run_brain_session persists the session id
    monkeypatch.setattr(bs, "_STREAM_EDIT_INTERVAL_S", 0.01)
    monkeypatch.setattr(bs, "_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(settings, "brain_timeout_s", 10)


# ---------------------------------------------------------------------------
# The stall case: nothing streams for a long time.
# ---------------------------------------------------------------------------


async def test_silent_stall_shows_elapsed_and_keeps_ticking():
    slack = FakeSlack()
    result, _ = await _run([(0.35, None), (0, _result("done"))], slack, "T_stall")

    assert result.reply == "done"
    waits = [u for u in slack.updates if "still working" in u]
    assert len(waits) >= 3, slack.updates  # ~0.35s / 0.05s tick


async def test_no_heartbeat_when_the_turn_is_fast():
    slack = FakeSlack()
    await _run([(0, _result("quick"))], slack, "T_fast")

    assert not any("still working" in u for u in slack.updates)


async def test_heartbeat_stops_after_the_turn_returns():
    slack = FakeSlack()
    await _run([(0.1, None), (0, _result("done"))], slack, "T_stop")
    count = len(slack.updates)
    await asyncio.sleep(0.2)  # several heartbeat intervals

    assert len(slack.updates) == count  # task cancelled, no stray edits


# ---------------------------------------------------------------------------
# Streaming: prose, tool progress, and a stall *after* partial content.
# ---------------------------------------------------------------------------


async def test_partial_prose_is_streamed_into_the_placeholder():
    slack = FakeSlack()
    script = [
        (0, _assistant(TextBlock("Answer part 1."))),
        (0.05, _assistant(TextBlock(" Part 2."))),
        (0, _result("Answer part 1. Part 2.")),
    ]
    await _run(script, slack, "T_stream")

    assert any(u.startswith("Answer part 1.") for u in slack.updates)
    assert any("Part 2." in u for u in slack.updates)


async def test_tool_phase_shows_the_running_tool_before_any_prose():
    slack = FakeSlack()
    script = [
        (0, _assistant(ToolUseBlock("t1", "mcp__agentic__db_query", {"sql": "x"}))),
        (0.05, _assistant(ToolUseBlock("t2", "Bash", {"command": "ls"}))),
        (0, _result("done")),
    ]
    result, _ = await _run(script, slack, "T_tools")

    assert result.tool_use_count == 2
    assert any("running `db_query`" in u for u in slack.updates)
    assert any("2 steps" in u for u in slack.updates)


async def test_stall_after_partial_prose_keeps_the_text_and_adds_elapsed():
    slack = FakeSlack()
    script = [
        (0, _assistant(TextBlock("Half an answer."))),
        (0.2, None),  # API goes quiet mid-turn
        (0, _result("Half an answer. Rest.")),
    ]
    await _run(script, slack, "T_midstall")

    stalled = [u for u in slack.updates if "still working" in u]
    assert stalled, slack.updates
    # The partial answer must survive the heartbeat edit, not be replaced by it.
    assert all(u.startswith("Half an answer.") for u in stalled)


async def test_token_deltas_type_the_answer_out_live():
    """With include_partial_messages the SDK emits StreamEvents before the
    AssistantMessage lands — the placeholder must grow token by token."""
    slack = FakeSlack()
    script = [
        (0, _delta("Hel")),
        (0.02, _delta("lo wor")),
        (0.02, _delta("ld")),
        (0, _assistant(TextBlock("Hello world"))),
        (0, _result("Hello world")),
    ]
    await _run(script, slack, "T_delta")

    prose = [u for u in slack.updates if "waiting" not in u]
    assert prose[0].startswith("Hel")
    assert any(u.startswith("Hello wor") for u in prose)


async def test_debounced_tokens_still_land_when_the_stream_pauses(monkeypatch):
    """The debounce must hold the newest view, not drop it. Real repro: the model
    types "I'll check." then goes off to run a tool for 2s — every token after the
    first landed inside the debounce window, so Slack sat on "I'll" the whole time.
    Uses an interval far larger than the gaps between deltas, which the compressed
    fixture timings (0.01s vs 0.02s deltas) never exercise."""
    monkeypatch.setattr(bs, "_STREAM_EDIT_INTERVAL_S", 0.15)
    # Keep the production ordering (1.5s stream vs 20s heartbeat): the fixture's
    # compressed 0.05s heartbeat would keep resetting the shared edit clock.
    monkeypatch.setattr(bs, "_HEARTBEAT_INTERVAL_S", 5)
    slack = FakeSlack()
    script = [
        (0, _delta("I'll ")),        # leading edge — pushed immediately
        (0.01, _delta("check.")),    # inside the window
        (0.6, _result("done")),      # the model is busy in a tool; nothing streams
    ]
    await _run(script, slack, "T_trailing")

    prose = [u for u in slack.updates if "waiting" not in u]
    assert any(u.startswith("I'll check.") for u in prose), prose


async def test_subagent_deltas_do_not_leak_into_the_placeholder():
    slack = FakeSlack()
    script = [
        (0, _delta("dev agent chatter", parent_tool_use_id="t1")),
        (0.02, _delta("real answer")),
        (0, _result("real answer")),
    ]
    await _run(script, slack, "T_subdelta")

    assert not any("chatter" in u for u in slack.updates)
    assert any(u.startswith("real answer") for u in slack.updates)


async def test_identical_content_is_not_re_pushed():
    slack = FakeSlack()
    same = _assistant(TextBlock(""))  # no text, no tool → nothing to render
    script = [(0, same), (0.02, same), (0, _result("done"))]
    await _run(script, slack, "T_dedupe")

    assert not [u for u in slack.updates if "waiting" not in u]


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------


async def test_slack_rate_limit_does_not_break_the_turn():
    slack = FakeSlack(rate_limit_after=1)
    script = [
        (0, _assistant(TextBlock("one"))),
        (0.05, _assistant(TextBlock(" two"))),
        (0.05, _assistant(TextBlock(" three"))),
        (0, _result("one two three")),
    ]
    result, _ = await _run(script, slack, "T_429")

    assert result.reply == "one two three"  # 429s swallowed, reply intact
    assert len(slack.updates) == 1  # backoff held further edits


async def test_timeout_during_a_stall_releases_the_client(monkeypatch):
    monkeypatch.setattr(settings, "brain_timeout_s", 0.15)
    slack = FakeSlack()
    result, pool = await _run([(5, _result("never"))], slack, "T_timeout")

    assert result.error and "timed out" in result.error
    assert pool.released == ["T_timeout"]
    # The user still saw progress instead of a frozen placeholder.
    assert any("still working" in u for u in slack.updates)


async def test_heartbeat_survives_a_failing_slack_edit():
    slack = FakeSlack(rate_limit_after=0)  # every edit raises
    result, _ = await _run([(0.15, None), (0, _result("ok"))], slack, "T_slackdown")

    assert result.reply == "ok"
    assert slack.updates == []


# ---------------------------------------------------------------------------
# Footer honesty (the "1 tool" that was really 0).
# ---------------------------------------------------------------------------


def test_footer_omits_the_tool_segment_when_no_tool_ran():
    from agentic.dispatcher import _footer

    out = _footer(0, 46.4, usage={"cache_read_input_tokens": 42744,
                                  "output_tokens": 917}, cost_usd=0.5457)
    assert "tool" not in out
    assert "46.4s" in out and "42k/0k tok" in out and "$0.546" in out


def test_footer_counts_real_tools():
    from agentic.dispatcher import _footer

    assert "1 tool ·" in _footer(1, 1.0)
    assert "3 tools ·" in _footer(3, 1.0)


async def test_reply_carries_a_footer_even_with_zero_tools(monkeypatch):
    from agentic import dispatcher
    from agentic.sdk.brain_session import BrainResult

    monkeypatch.setattr(dispatcher, "_brain_pool_singleton", lambda: object())
    monkeypatch.setattr(dispatcher, "_pending_singleton", lambda: object())

    async def fake_run(**_kw):
        return BrainResult(
            reply="table here", session_id="s", usage={"output_tokens": 900},
            cost_usd=0.55, duration_ms=46434, num_turns=1, tool_use_count=0,
        )

    monkeypatch.setattr(dispatcher, "run_brain_session", fake_run)
    out = await dispatcher.handle_message(
        "again", thread_ts="T_footer", channel="C1", user_id="U1",
        slack_client=object(), placeholder_ts="1.1",
    )

    assert "table here" in out
    assert "🛠️" in out and "tool" not in out
