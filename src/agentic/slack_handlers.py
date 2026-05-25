import logging
import re

from slack_bolt.async_app import AsyncApp

from .config import settings
from .worker import Job, JobRunner

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")
_BUSY_MSG = "⏳ Đang chạy job trước rồi, đợi xíu nha."

_channel_name_cache: dict[str, str] = {}


def _clean(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


async def _channel_name(client, channel_id: str) -> str | None:
    cached = _channel_name_cache.get(channel_id)
    if cached is not None:
        return cached
    try:
        resp = await client.conversations_info(channel=channel_id)
        name = (resp.get("channel") or {}).get("name") or ""
    except Exception as e:
        log.warning("conversations_info failed for %s: %s", channel_id, e)
        return None
    _channel_name_cache[channel_id] = name.lower()
    return _channel_name_cache[channel_id]


def register(app: AsyncApp, runner: JobRunner) -> None:
    allowed = settings.allowed_channel_names

    async def _is_allowed(client, channel_id: str) -> bool:
        if not allowed:
            return True
        name = await _channel_name(client, channel_id)
        return name in allowed if name else False

    @app.event("app_mention")
    async def on_mention(event, client):
        channel = event["channel"]
        if not await _is_allowed(client, channel):
            log.info("ignoring mention from non-allowlisted channel %s", channel)
            return

        raw = event.get("text") or ""
        text = _clean(raw)
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user")

        if not text:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Bạn cần gì? Mention mình kèm nội dung nha.",
            )
            return

        placeholder = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="Đang xử lý..."
        )

        async def reply(msg: str) -> None:
            # Slack chat.update rejects long text with msg_too_long well below
            # the documented 40k limit; dispatcher already summarizes, this is
            # the last-resort safety net.
            safe = msg if len(msg) <= 1000 else msg[:980] + "\n…(cắt)"
            await client.chat_update(
                channel=channel, ts=placeholder["ts"], text=safe
            )

        job = Job(
            text=text,
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            reply=reply,
        )
        accepted = await runner.submit(job)
        if not accepted:
            await reply(_BUSY_MSG)

    @app.event("message")
    async def on_message(event, client):
        # DM disabled by policy; ignore everything that isn't an app_mention.
        return
