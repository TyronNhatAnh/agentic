"""Placeholder heartbeat + busy-elapsed reporting (2026-08-07 stall)."""

import asyncio
import time

from agentic.sdk import brain_session as bs
from agentic.worker import Job, JobRunner


class FakeSlack:
    def __init__(self):
        self.updates = []

    async def chat_update(self, *, channel, ts, text, blocks):
        self.updates.append(blocks[0]["text"])


async def test_heartbeat_edits_placeholder_while_stream_is_silent():
    slack = FakeSlack()
    progress = bs._Progress(slack, "C1", "1.0", time.monotonic() - 125)
    stop = asyncio.Event()
    hb = asyncio.create_task(bs._heartbeat(progress, stop))
    # Drive the heartbeat without waiting out the real interval.
    await asyncio.sleep(0)
    await progress.render_heartbeat()
    stop.set()
    await hb

    assert len(slack.updates) == 1
    assert "waiting for the model… 2m05s" in slack.updates[0]


async def test_heartbeat_backs_off_when_stream_just_rendered():
    slack = FakeSlack()
    progress = bs._Progress(slack, "C1", "1.0", time.monotonic())
    await progress.render("partial answer")
    await progress.render_heartbeat()  # inside the stale window → skipped

    assert slack.updates == ["partial answer" + bs._STREAM_SUFFIX]


async def test_busy_reply_reports_elapsed():
    async def handler(*args, **kwargs):
        await asyncio.sleep(0.05)
        return "done"

    runner = JobRunner(handler, concurrency=1)
    runner.start()

    async def noop(_):
        return None

    job = Job(text="a", thread_ts="t1", channel="C1", user_id="U1", reply=noop)
    assert await runner.submit(job) is True
    assert await runner.submit(job) is False  # same thread still in flight
    elapsed = runner.busy_elapsed_s("t1")
    assert elapsed is not None and elapsed >= 0

    await runner._queue.join()
    assert runner.busy_elapsed_s("t1") is None


async def test_other_threads_are_not_blocked_by_a_busy_one():
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(*args, **kwargs):
        started.set()
        await release.wait()
        return "done"

    runner = JobRunner(handler, concurrency=2)
    runner.start()

    async def noop(_):
        return None

    slow = Job(text="a", thread_ts="t1", channel="C1", user_id="U1", reply=noop)
    other = Job(text="b", thread_ts="t2", channel="C1", user_id="U1", reply=noop)
    assert await runner.submit(slow) is True
    await started.wait()
    assert await runner.submit(other) is True  # different thread → accepted
    assert await runner.submit(slow) is False  # same thread → rejected
    release.set()
    await runner._queue.join()


def test_busy_msg_formats_minutes_and_seconds():
    from agentic.slack_handlers import _BUSY_MSG, _busy_msg

    assert _busy_msg(None) == _BUSY_MSG  # unknown start → no fake precision
    assert "(12s so far)" in _busy_msg(12.4)
    assert "(6m12s so far)" in _busy_msg(372.9)
