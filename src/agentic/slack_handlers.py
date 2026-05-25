import logging
import re

from slack_bolt.async_app import AsyncApp

from .config import settings
from .worker import Job, JobRunner

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")
_BUSY_MSG = "⏳ Đang chạy job trước rồi, đợi xíu nha."
_SLACK_CHUNK_LEN = 3500

_channel_name_cache: dict[str, str] = {}


def _clean(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def _placeholder_for(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in ("fix", "sửa", "sua", "patch")) and (
        "pr" in lowered or "pull/" in lowered
    ):
        return "⏳ Đang chuẩn bị PR worktree để fix..."
    if "review" in lowered and ("pr" in lowered or "pull/" in lowered):
        return "⏳ Đang fetch diff và review code..."
    return "⏳ Đang xử lý..."


def _progress_messages_for(text: str) -> list[str]:
    lowered = text.lower()
    if any(w in lowered for w in ("fix", "sửa", "sua", "patch")) and (
        "pr" in lowered or "pull/" in lowered
    ):
        return [
            "⏳ Đang fetch PR diff và chuẩn bị worktree...",
            "🛠️ Đang để Claude Code đọc repo và sửa file...",
            "🧪 Đang đợi verify/test hoặc tổng hợp kết quả...",
        ]
    if "review" in lowered and ("pr" in lowered or "pull/" in lowered):
        return [
            "⏳ Đang fetch PR diff...",
            "🔎 Đang review trên local worktree nếu có...",
            "📝 Đang tổng hợp findings...",
        ]
    return [
        "⏳ Đang nghĩ tiếp...",
        "⏳ Vẫn đang xử lý, đợi xíu nha...",
    ]


def _to_slack_mrkdwn(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            prefix_len = len(line) - len(stripped)
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if 1 <= hashes <= 6 and stripped[hashes:].startswith(" "):
                line = line[:prefix_len] + stripped[hashes + 1 :]
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)


def _chunks(text: str, limit: int = _SLACK_CHUNK_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


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
            channel=channel,
            thread_ts=thread_ts,
            text=_placeholder_for(text),
        )

        async def reply(msg: str) -> None:
            msg = _to_slack_mrkdwn(msg)
            parts = _chunks(msg)
            await client.chat_update(
                channel=channel, ts=placeholder["ts"], text=parts[0]
            )
            for part in parts[1:]:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=part,
                )

        async def progress(msg: str) -> None:
            await client.chat_update(
                channel=channel,
                ts=placeholder["ts"],
                text=_to_slack_mrkdwn(msg),
            )

        job = Job(
            text=text,
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            reply=reply,
            progress=progress,
            progress_messages=_progress_messages_for(text),
        )
        accepted = await runner.submit(job)
        if not accepted:
            await reply(_BUSY_MSG)

    @app.event("message")
    async def on_message(event, client):
        # DM disabled by policy; ignore everything that isn't an app_mention.
        return
