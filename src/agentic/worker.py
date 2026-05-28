import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

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
    progress: ReplyFn | None = None
    progress_messages: list[str] = field(default_factory=list)
    thread_history: list[dict] = field(default_factory=list)


class JobRunner:
    def __init__(self, handler: HandlerFn, concurrency: int = 4) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._busy: set[str] = set()
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
        self._busy.add(job.thread_ts)
        await self._queue.put(job)
        return True

    async def _worker(self, idx: int) -> None:
        log.info("worker %d started", idx)
        while True:
            job = await self._queue.get()
            progress_task: asyncio.Task | None = None
            try:
                if job.progress:
                    progress_task = asyncio.create_task(
                        self._progress_loop(job), name=f"agentic-progress-{idx}"
                    )
                reply = await self._handler(
                    job.text,
                    thread_ts=job.thread_ts,
                    channel=job.channel,
                    user_id=job.user_id,
                    thread_history=job.thread_history,
                    progress=job.progress,
                )
            except Exception as e:
                log.exception("worker %d handler error", idx)
                reply = f"❌ {e}"
            try:
                await job.reply(reply)
            except Exception as e:
                log.exception("worker %d reply failed", idx)
                # Placeholder ("Đang xử lý...") sẽ kẹt nếu không update lại.
                # Thử lần nữa với message ngắn báo lỗi để user biết.
                try:
                    await job.reply(f"❌ Lỗi gửi phản hồi: {type(e).__name__}")
                except Exception:
                    log.exception("worker %d fallback reply also failed", idx)
            finally:
                if progress_task:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass
                self._busy.discard(job.thread_ts)
                self._queue.task_done()

    async def _progress_loop(self, job: Job) -> None:
        messages = job.progress_messages or ["⏳ Đang xử lý..."]
        try:
            for i, msg in enumerate(messages):
                await asyncio.sleep(5 if i == 0 else 10)
                if job.progress:
                    await job.progress(msg)
            i = len(messages)
            while True:
                await asyncio.sleep(10)
                if job.progress:
                    await job.progress(messages[-1])
                i += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("progress update failed")
