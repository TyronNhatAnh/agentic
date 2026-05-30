"""Thread-history rendering for the brain user message.

`_format_messages` moved into sdk/brain_session.py at the Phase 5 cutover (the
JSON-decision parser it used to live beside is gone — the brain emits native
tool_use blocks now, so there is nothing to parse)."""

from agentic.sdk.brain_session import _format_messages


def test_format_messages_keeps_useful_thread_history():
    review = "🔍 **Review: repo/service#431**\n\n### Blocking issues\n" + ("finding\n" * 100)

    out = _format_messages(
        [
            {"role": "user", "text": "review pr https://github.com/repo/service/pull/431"},
            {"role": "assistant", "text": review},
        ]
    )

    assert "repo/service#431" in out
    assert "Blocking issues" in out
    assert len(out) > 400


def test_format_messages_keeps_long_analysis_for_reuse():
    # A long in-thread analysis (the bot's own earlier breakdown) must survive
    # into the brain's view so it can be reused as a fix spec, not truncated away.
    analysis = "Lock assign order trong ggx-kr-da-api:\n" + ("chi tiết phân tích\n" * 300)
    assert len(analysis) > 4000  # well past the old 2400-char per-message cap

    out = _format_messages([{"role": "assistant", "text": analysis}])

    assert "ggx-kr-da-api" in out
    assert "message cắt bớt" not in out
    assert len(out) >= len(analysis)
