import logging
import re
import time
from collections import OrderedDict

from slack_bolt.async_app import AsyncApp

from .config import settings
from .sdk import PendingPermissions
from .worker import Job, JobRunner

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>\s*")
_BUSY_MSG = "⏳ Still running a previous job, hang on."


def _busy_msg(elapsed_s: float | None) -> str:
    """Bare "busy" reads the same whether the job started 5s or 6min ago — the
    elapsed time is what tells the user it's stalled rather than working."""
    if elapsed_s is None:
        return _BUSY_MSG
    s = int(elapsed_s)
    pretty = f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
    return f"⏳ Still running a previous job ({pretty} so far), hang on."
# Block Kit markdown blocks allow 12,000 chars cumulatively per payload; one
# block per message, kept just under the cap to leave room for any suffix.
_SLACK_CHUNK_LEN = 11800
_CHANNEL_CACHE_TTL_S = 30 * 60  # 30 minutes — picks up renames within half an hour
_USER_CACHE_TTL_S = 30 * 60
# Hard ceiling on cache entries. Growth is naturally bounded by the workspace's
# user/channel count, but a long-lived process in a large workspace should not
# keep every identity it ever saw — evict least-recently-used past this cap.
_CACHE_MAX_ENTRIES = 2000

# Both are LRU: most-recently-used at the end, oldest evicted first past the cap.
_channel_name_cache: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
# uid -> (display_name, email, fetched_at)
_user_info_cache: "OrderedDict[str, tuple[str | None, str | None, float]]" = OrderedDict()
_bot_user_id_cache: dict[str, str | None] = {}


def _cache_put(cache: OrderedDict, key: str, value: tuple) -> None:
    """Insert/refresh an LRU entry and evict the oldest beyond the cap."""
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAX_ENTRIES:
        cache.popitem(last=False)


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
        _user_info_cache.move_to_end(user_id)  # mark recently used
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
        _cache_put(_user_info_cache, user_id, (name, email, now))
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
        return "⏳ Preparing the PR worktree to fix..."
    if "review" in lowered and ("pr" in lowered or "pull/" in lowered):
        return "⏳ Fetching the diff and reviewing the code..."
    return "⏳ Processing..."


def _markdown_block(text: str) -> list[dict]:
    """Wrap text in a Block Kit ``markdown`` block. Slack converts standard
    GitHub-flavoured markdown server-side — headings, ordered/nested lists,
    tables, fenced code with syntax highlight, dividers, task lists — so we no
    longer translate to legacy mrkdwn ourselves (which silently dropped most of
    those). Verified working via chat.postMessage and chat.update for this app."""
    return [{"type": "markdown", "text": text}]


def _notify_text(text: str) -> str:
    """Short plaintext fallback for the `text` field when sending blocks. Slack
    uses it for push/desktop notifications and accessibility; the visible body
    comes from the markdown block."""
    stripped = text.strip()
    if not stripped:
        return "message"
    return stripped.splitlines()[0][:150]


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
        # On a status-200 ok:false (missing_scope / not_in_channel / etc.) the
        # generic SlackApiError string hides the cause — surface response.error
        # (+ needed/provided for scope errors) so the real reason is visible.
        resp_err = getattr(getattr(e, "response", None), "data", None) or {}
        if not isinstance(resp_err, dict):
            resp_err = {}
        detail = resp_err.get("error") or str(e)
        scope = ""
        if resp_err.get("needed") or resp_err.get("provided"):
            scope = f" (needed={resp_err.get('needed')} provided={resp_err.get('provided')})"
        log.warning(
            "conversations_replies failed for %s: %s%s", thread_ts, detail, scope
        )
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
        _channel_name_cache.move_to_end(channel_id)  # mark recently used
        return cached[0]
    try:
        resp = await client.conversations_info(channel=channel_id)
        name = (resp.get("channel") or {}).get("name") or ""
    except Exception as e:
        log.warning("conversations_info failed for %s: %s", channel_id, e)
        # Reuse stale cache on transient failure rather than denying access.
        return cached[0] if cached else None
    _cache_put(_channel_name_cache, channel_id, (name.lower(), now))
    return _channel_name_cache[channel_id][0]


def register(
    app: AsyncApp,
    runner: JobRunner,
    *,
    pending: PendingPermissions | None = None,
) -> None:
    """Wire Slack events + SDK permission button actions.

    `pending` is the singleton PendingPermissions instance from main.py. It can
    be None when running under tests / before Phase 1 wiring; in that case the
    perm_allow / perm_deny buttons simply ack with no effect.
    """
    allowed = settings.allowed_channel_names

    async def _is_allowed(client, channel_id: str) -> bool:
        if not allowed:
            return True
        if channel_id in allowed:
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
                text="What do you need? Mention me with your request.",
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
            parts = _chunks(msg)
            await client.chat_update(
                channel=channel,
                ts=placeholder["ts"],
                text=_notify_text(parts[0]),
                blocks=_markdown_block(parts[0]),
            )
            for part in parts[1:]:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=_notify_text(part),
                    blocks=_markdown_block(part),
                )

        job = Job(
            text=text,
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
            reply=reply,
            thread_history=thread_history,
            slack_client=client,
            placeholder_ts=placeholder["ts"],
        )
        accepted = await runner.submit(job)
        if not accepted:
            await reply(_busy_msg(runner.busy_elapsed_s(thread_ts)))

    @app.event("message")
    async def on_message(event, client):
        # Mention required everywhere; non-mention messages are ignored.
        return

    @app.action("perm_allow")
    async def on_perm_allow(ack, body, client):
        await ack()
        await _resolve_permission(client, body, allow=True)

    @app.action("perm_deny")
    async def on_perm_deny(ack, body, client):
        await ack()
        await _resolve_permission(client, body, allow=False)

    async def _resolve_permission(client, body: dict, *, allow: bool) -> None:
        # value carries `req_id` we set when posting the buttons.
        try:
            req_id = body["actions"][0]["value"]
        except (KeyError, IndexError):
            log.warning("perm action missing req_id: %s", body)
            return
        verdict = pending.resolve(req_id, allow) if pending else False
        # Replace the button message with a static decision marker so the user
        # sees their click reflected and the buttons can't be re-pressed.
        message = body.get("message") or {}
        channel = (body.get("channel") or {}).get("id") or ""
        ts = message.get("ts")
        if not (channel and ts):
            return
        label = "✅ Allowed" if allow else "❌ Cancelled"
        if not verdict:
            # Future already resolved or expired — still update text so the UI
            # doesn't look stuck.
            label = f"{label} (request expired)"
        try:
            await client.chat_update(channel=channel, ts=ts, text=label, blocks=[])
        except Exception:
            log.exception("perm chat_update failed req=%s", req_id)
