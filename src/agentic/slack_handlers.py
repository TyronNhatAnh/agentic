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
_BUSY_MSG = "⏳ Đang chạy job trước rồi, đợi xíu nha."
_SLACK_CHUNK_LEN = 3500
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
        return "⏳ Đang chuẩn bị PR worktree để fix..."
    if "review" in lowered and ("pr" in lowered or "pull/" in lowered):
        return "⏳ Đang fetch diff và review code..."
    return "⏳ Đang xử lý..."


# A GFM table separator row, e.g. `|---|:--:|` (the line under the header).
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# `[label](http://url)` — converted to Slack's `<url|label>` link syntax.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _inline_mrkdwn(line: str) -> str:
    """Inline GitHub-markdown → Slack-mrkdwn on a single non-table line."""
    line = _MD_LINK_RE.sub(r"<\2|\1>", line)
    line = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", line)
    line = re.sub(r"__([^_\n]+)__", r"*\1*", line)
    line = re.sub(r"~~([^~\n]+)~~", r"~\1~", line)
    return line


def _strip_inline_md(cell: str) -> str:
    """Flatten inline markup inside a table cell — Slack does not render mrkdwn
    inside ``` code blocks, so `**x**`/`` `x` ``/links would otherwise show raw."""
    cell = _MD_LINK_RE.sub(r"\1 (\2)", cell)
    cell = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", cell)
    cell = cell.replace("**", "").replace("`", "")
    return cell.strip()


def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _render_table(rows: list[str]) -> list[str]:
    """Render a markdown table (header + body lines, separator already dropped)
    into a monospace code block so columns align on Slack, which has no tables."""
    parsed = [[_strip_inline_md(c) for c in _split_table_row(r)] for r in rows]
    ncol = max(len(r) for r in parsed)
    for r in parsed:
        r.extend([""] * (ncol - len(r)))
    widths = [max(len(r[i]) for r in parsed) for i in range(ncol)]
    out = ["```"]
    for r in parsed:
        out.append(" | ".join(r[i].ljust(widths[i]) for i in range(ncol)).rstrip())
    out.append("```")
    return out


def _to_slack_mrkdwn(text: str) -> str:
    src = text.splitlines()
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        line = src[i]
        # GFM table: a `|`-row immediately followed by a separator row. Collect
        # the header + all following pipe rows and render as one code block.
        if (
            "|" in line
            and i + 1 < n
            and "|" in src[i + 1]
            and _TABLE_SEP_RE.match(src[i + 1])
        ):
            block = [line]
            j = i + 2
            while j < n and "|" in src[j] and src[j].strip():
                block.append(src[j])
                j += 1
            out.extend(_render_table(block))
            i = j
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            prefix_len = len(line) - len(stripped)
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if 1 <= hashes <= 6 and stripped[hashes:].startswith(" "):
                line = line[:prefix_len] + stripped[hashes + 1 :]
        out.append(_inline_mrkdwn(line))
        i += 1
    return "\n".join(out)


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
            await reply(_BUSY_MSG)

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
        label = "✅ Đã cho phép" if allow else "❌ Đã huỷ"
        if not verdict:
            # Future already resolved or expired — still update text so the UI
            # doesn't look stuck.
            label = f"{label} (request đã hết hạn)"
        try:
            await client.chat_update(channel=channel, ts=ts, text=label, blocks=[])
        except Exception:
            log.exception("perm chat_update failed req=%s", req_id)
