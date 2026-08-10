import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

ReplyFn = Callable[[str], Awaitable[None]]
HandlerFn = Callable[..., Awaitable[str]]


@dataclass
class Job:
    text: str
    thread_ts: str
    channel: str
    user_id: str | None
    reply: ReplyFn
    thread_history: list[dict] = field(default_factory=list)
    # The brain session streams partial replies straight into the placeholder
    # and posts permission-button messages, so it needs raw Slack access.
    slack_client: Any = None
    placeholder_ts: str | None = None


class JobRunner:
    def __init__(self, handler: HandlerFn, concurrency: int = 4) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        # thread_ts -> submit time, so a rejected message can tell the user how
        # long the in-flight job has been running instead of a bare "busy".
        self._busy: dict[str, float] = {}
        self._workers: list[asyncio.Task] = []
        self._concurrency = concurrency

    def start(self) -> None:
        for i in range(self._concurrency):
            self._workers.append(
                asyncio.create_task(self._worker(i), name=f"agentic-worker-{i}")
            )

    async def submit(self, job: Job) -> bool:
        """Returns False if the thread already has a job in flight."""
        if job.thread_ts in self._busy:
            return False
        self._busy[job.thread_ts] = time.monotonic()
        await self._queue.put(job)
        return True

    def busy_elapsed_s(self, thread_ts: str) -> float | None:
        """Seconds the thread's in-flight job has been running, None if idle."""
        started = self._busy.get(thread_ts)
        return None if started is None else time.monotonic() - started

    async def _worker(self, idx: int) -> None:
        log.info("worker %d started", idx)
        while True:
            job = await self._queue.get()
            try:
                reply = await self._handler(
                    job.text,
                    thread_ts=job.thread_ts,
                    channel=job.channel,
                    user_id=job.user_id,
                    thread_history=job.thread_history,
                    slack_client=job.slack_client,
                    placeholder_ts=job.placeholder_ts,
                )
            except Exception as e:
                log.exception("worker %d handler error", idx)
                reply = f"❌ {e}"
            try:
                await job.reply(reply)
            except Exception as e:
                log.exception("worker %d reply failed", idx)
                # Placeholder ("Processing...") stays stuck if we don't update it.
                # Retry with a short error message so the user knows.
                try:
                    await job.reply(f"❌ Failed to send reply: {type(e).__name__}")
                except Exception:
                    log.exception("worker %d fallback reply also failed", idx)
            finally:
                self._busy.pop(job.thread_ts, None)
                self._queue.task_done()
