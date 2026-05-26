import logging
import re
import time

from slack_bolt.async_app import AsyncApp

from .config import settings
from .worker import Job, JobRunner

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>\s*")
_BUSY_MSG = "⏳ Đang chạy job trước rồi, đợi xíu nha."
_SLACK_CHUNK_LEN = 3500
_CHANNEL_CACHE_TTL_S = 30 * 60  # 30 minutes — picks up renames within half an hour
_USER_CACHE_TTL_S = 30 * 60

_channel_name_cache: dict[str, tuple[str, float]] = {}
# uid -> (display_name, email, fetched_at)
_user_info_cache: dict[str, tuple[str | None, str | None, float]] = {}
_bot_user_id_cache: dict[str, str | None] = {}


async def _bot_user_id(client) -> str | None:
    """Cached `auth.test` — needed to distinguish the bot's own mention from
    mentions of other users so we only strip ours and keep theirs as context."""
    if "id" in _bot_user_id_cache:
        return _bot_user_id_cache["id"]
    try:
        resp = await client.auth_test()
        uid = resp.get("user_id") or None
    except Exception as e:
        log.warning("auth_test failed: %s", e)
        return None
    _bot_user_id_cache["id"] = uid
    return uid


async def _user_label(client, user_id: str) -> str | None:
    cached = _user_info_cache.get(user_id)
    now = time.time()
    if cached is not None and now - cached[2] < _USER_CACHE_TTL_S:
        name, email = cached[0], cached[1]
    else:
        name: str | None = None
        email: str | None = None
        try:
            resp = await client.users_info(user=user_id)
            user = resp.get("user") or {}
            profile = user.get("profile") or {}
            name = (
                profile.get("real_name")
                or profile.get("display_name")
                or user.get("real_name")
                or user.get("name")
            )
            email = profile.get("email")
        except Exception as e:
            log.warning("users_info failed for %s: %s", user_id, e)
            if cached is not None:
                name, email = cached[0], cached[1]  # reuse stale on transient failure
        _user_info_cache[user_id] = (name, email, now)
    if not name:
        return None
    return f"@{name} ({email})" if email else f"@{name}"


async def _resolve_mentions(client, text: str, bot_user_id: str | None) -> str:
    """Strip the bot's own mention; replace other `<@USERID>` with
    `@DisplayName (email)` so the brain has actual identity context to work
    with (search Jira by email, look up GitHub by name, etc.)."""
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for m in _MENTION_RE.finditer(text):
        uid = m.group(1)
        parts.append(text[last : m.start()])
        if bot_user_id and uid == bot_user_id:
            replacement = ""
        else:
            label = await _user_label(client, uid)
            replacement = f"{label} " if label else ""
        parts.append(replacement)
        last = m.end()
    parts.append(text[last:])
    return "".join(parts).strip()


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


async def _fetch_thread_history(
    client,
    channel: str,
    thread_ts: str,
    current_ts: str,
    bot_user_id: str | None,
    limit: int = 20,
) -> list[dict]:
    """Pull the last ~20 messages in this thread from Slack and normalize them
    for the brain. Filters out the current mention (and anything posted after it).
    Bot-posted messages map to role="assistant", everything else to "user".

    Slack is the source of truth here because non-mention user messages (other
    teammates pasting links, context, etc.) never reach the DB."""
    try:
        resp = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=limit
        )
    except Exception as e:
        log.warning("conversations_replies failed for %s: %s", thread_ts, e)
        return []
    try:
        current_ts_f = float(current_ts)
    except (TypeError, ValueError):
        current_ts_f = None
    history: list[dict] = []
    for m in resp.get("messages") or []:
        msg_ts = m.get("ts")
        if msg_ts == current_ts:
            continue
        if current_ts_f is not None:
            try:
                if float(msg_ts) >= current_ts_f:
                    continue
            except (TypeError, ValueError):
                pass
        text = await _resolve_mentions(client, m.get("text") or "", bot_user_id)
        if not text:
            continue
        role = "assistant" if m.get("bot_id") else "user"
        history.append({"role": role, "text": text})
    return history[-10:]


async def _channel_name(client, channel_id: str) -> str | None:
    cached = _channel_name_cache.get(channel_id)
    now = time.time()
    if cached is not None and now - cached[1] < _CHANNEL_CACHE_TTL_S:
        return cached[0]
    try:
        resp = await client.conversations_info(channel=channel_id)
        name = (resp.get("channel") or {}).get("name") or ""
    except Exception as e:
        log.warning("conversations_info failed for %s: %s", channel_id, e)
        # Reuse stale cache on transient failure rather than denying access.
        return cached[0] if cached else None
    _channel_name_cache[channel_id] = (name.lower(), now)
    return _channel_name_cache[channel_id][0]


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
        thread_ts = event.get("thread_ts") or event["ts"]
        current_ts = event["ts"]
        user_id = event.get("user")

        bot_uid = await _bot_user_id(client)
        text = await _resolve_mentions(client, raw, bot_uid)

        if not text:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Bạn cần gì? Mention mình kèm nội dung nha.",
            )
            return

        thread_history = await _fetch_thread_history(
            client, channel, thread_ts, current_ts, bot_uid
        )

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
            thread_history=thread_history,
        )
        accepted = await runner.submit(job)
        if not accepted:
            await reply(_BUSY_MSG)

    @app.event("message")
    async def on_message(event, client):
        # DM disabled by policy; ignore everything that isn't an app_mention.
        return
