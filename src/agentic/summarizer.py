import asyncio
import logging

from .agents.base import run_claude
from .store import count_messages, get_thread, recent_messages, update_thread_fields

log = logging.getLogger(__name__)

MSG_THRESHOLD = 20
CHAR_THRESHOLD = 8000
SUMMARY_TIMEOUT = 60
TAIL_KEEP = 5  # messages to leave un-summarized so they still appear verbatim

_PROMPT = """Bạn là trợ lý tóm tắt hội thoại Slack giữa user (Tyron) và bot.

Viết tóm tắt ngắn (~150 từ, tiếng Việt) gồm các điểm còn liên quan:
- User đang làm gì: ticket / project / feature / repo.
- Repo mặc định nếu có nhắc đến.
- Jira keys, PR numbers đã xuất hiện.
- Quyết định đã chốt, ràng buộc (vd: không auto-merge).
- Action đã thực hiện (tạo PR, comment, transition…).
- Trạng thái hiện tại / câu hỏi mở.

Chỉ trả văn bản thuần, không markdown fence, không tiêu đề."""

_busy: set[str] = set()
_scheduled: set[asyncio.Task] = set()


def _exceeds_threshold(thread_ts: str) -> bool:
    n = count_messages(thread_ts)
    if n > MSG_THRESHOLD:
        return True
    msgs = recent_messages(thread_ts, limit=1000)
    total = sum(len(m["text"]) for m in msgs)
    return total > CHAR_THRESHOLD


async def summarize_thread(thread_ts: str) -> None:
    if thread_ts in _busy:
        return
    _busy.add(thread_ts)
    try:
        msgs = recent_messages(thread_ts, limit=1000)
        if len(msgs) <= TAIL_KEEP:
            return
        to_summarize = msgs[:-TAIL_KEEP]
        prior = (get_thread(thread_ts) or {}).get("summary") or ""
        transcript = "\n".join(f"{m['role']}: {m['text']}" for m in to_summarize)
        user_prompt = (
            (f"Previous summary:\n{prior}\n\n" if prior else "")
            + f"Transcript cần tóm tắt:\n{transcript}"
        )
        summary = await run_claude(_PROMPT, user_prompt, timeout=SUMMARY_TIMEOUT)
        update_thread_fields(thread_ts, summary=summary.strip())
        log.info(
            "summarized thread=%s msgs=%d chars=%d",
            thread_ts,
            len(to_summarize),
            len(summary),
        )
    except Exception:
        log.exception("summarize failed thread=%s", thread_ts)
    finally:
        _busy.discard(thread_ts)


def maybe_schedule(thread_ts: str | None) -> None:
    """Fire-and-forget summarize if thread is over thresholds."""
    if not thread_ts:
        return
    if thread_ts in _busy:
        return
    if not _exceeds_threshold(thread_ts):
        return
    task = asyncio.create_task(
        summarize_thread(thread_ts), name=f"summarize-{thread_ts}"
    )
    _scheduled.add(task)
    task.add_done_callback(_scheduled.discard)
