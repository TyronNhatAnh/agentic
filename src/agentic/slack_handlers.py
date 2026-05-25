import logging
import re

from slack_bolt.async_app import AsyncApp

from .dispatcher import handle_message

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")


def _clean(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def register(app: AsyncApp) -> None:
    @app.event("app_mention")
    async def on_mention(event, say, client):
        text = _clean(event.get("text", ""))
        if not text:
            return
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user")

        placeholder = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="🧠 Đang xử lý..."
        )
        try:
            reply = await handle_message(
                text, thread_ts=thread_ts, channel=channel, user_id=user_id
            )
        except Exception as e:
            log.exception("dispatcher error")
            reply = f"❌ {e}"
        await client.chat_update(
            channel=channel, ts=placeholder["ts"], text=reply[:39000]
        )

    @app.event("message")
    async def on_dm(event, say, client):
        # Only DMs to the bot, ignore channel chatter and bot echoes.
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        text = (event.get("text") or "").strip()
        if not text:
            return
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user")

        placeholder = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="🧠 Đang xử lý..."
        )
        try:
            reply = await handle_message(
                text, thread_ts=thread_ts, channel=channel, user_id=user_id
            )
        except Exception as e:
            log.exception("dispatcher error")
            reply = f"❌ {e}"
        await client.chat_update(
            channel=channel, ts=placeholder["ts"], text=reply[:39000]
        )
