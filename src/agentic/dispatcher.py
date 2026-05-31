"""Request orchestrator — SDK-only (Phase 5 cutover).

`handle_message` is now thin: resolve any prepared worktree for the ticket in
play (so the brain can route fix/PR work to the dev sub-agent), then delegate
the whole turn to a long-lived `ClaudeSDKClient` via `run_brain_session`. The
brain orchestrates tools + sub-agents natively; Python only does Slack I/O,
workspace lookup, and persistence. The legacy `claude -p` ReAct loop, the
`AGENTIC_USE_SDK` flag, and the JSON-action path are gone.
"""

import json
import logging
import re
import time

from .config import settings
from .integrations import git as git_int
from .revamp_pipeline import run_revamp_pipeline
from .sdk import (
    PendingPermissions,
    ThreadSessionManager,
    run_brain_session,
)
from .store import (
    add_message,
    get_thread,
    list_services,
    log_run,
    recent_messages,
    resolve_service_by_github_repo,
    touch_thread,
    update_thread_fields,
)

log = logging.getLogger(__name__)


# SDK singletons. main.py calls `init_sdk_singletons(...)` after init_db; tests
# inject their own via monkeypatch and may leave these unset (None).
_brain_pool: ThreadSessionManager | None = None
_pending: PendingPermissions | None = None


def init_sdk_singletons(
    *,
    brain_pool: ThreadSessionManager,
    pending: PendingPermissions,
) -> None:
    global _pending, _brain_pool
    _pending = pending
    _brain_pool = brain_pool


def _brain_pool_singleton() -> ThreadSessionManager | None:
    return _brain_pool


def _pending_singleton() -> PendingPermissions | None:
    return _pending


def _footer(
    tool_count: int,
    elapsed_s: float,
    *,
    usage: dict | None = None,
    cost_usd: float | None = None,
) -> str:
    plural = "s" if tool_count != 1 else ""
    base = f"_🛠️ {tool_count} tool{plural} · {elapsed_s:.1f}s"
    if usage:
        in_tok = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        out_tok = usage.get("output_tokens", 0)
        base += f" · {in_tok // 1000}k/{out_tok // 1000}k tok"
    if cost_usd:
        base += f" · ${cost_usd:.3f}"
    return base + "_"


def _with_footer(
    reply: str,
    tool_count: int,
    t_start: float,
    *,
    usage: dict | None = None,
    cost_usd: float | None = None,
) -> str:
    if tool_count <= 0:
        return reply
    footer = _footer(tool_count, time.time() - t_start, usage=usage, cost_usd=cost_usd)
    return f"{reply}\n\n{footer}"


def _truncate(text: str, limit: int, *, label: str = "input") -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit - 200
    tail = 100
    truncated = (
        text[:head]
        + f"\n…[{label} cắt bớt {len(text) - head - tail} ký tự]…\n"
        + text[-tail:]
    )
    return truncated, True


def _service_slug_from_registry_text(text: str) -> str | None:
    """Find a registered service mentioned by bare name/alias in text.

    Returns the service's github_repo slug (or canonical name) so downstream
    resolution and thread.repo persistence stay slug-shaped. Only matches keys
    containing a hyphen (canonical names, `ggx-kr-*` aliases, github repo tails)
    so short generic aliases like "order"/"user" don't hijack a free-text match.
    """
    haystack = text.lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_.-]*", haystack))
    if not tokens:
        return None
    for svc in list_services():
        keys = {(svc.get("name") or "").lower()}
        gh = (svc.get("github_repo") or "").strip().lower()
        if gh:
            keys.add(gh.rsplit("/", 1)[-1])
        try:
            keys.update(a.lower() for a in json.loads(svc.get("aliases") or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        keys = {k for k in keys if "-" in k}
        if keys & tokens:
            return gh or (svc.get("name") or None)
    return None


def _service_slug_from_registry(text: str, messages: list[dict]) -> str | None:
    current = _service_slug_from_registry_text(text)
    if current:
        return current
    for msg in reversed(messages):
        found = _service_slug_from_registry_text(msg.get("text") or "")
        if found:
            return found
    return None


_REVAMP_CMD_RE = re.compile(r"^\s*revamp\s+(?P<scope>\S.*)$", re.IGNORECASE)


def _revamp_scope(text: str, channel: str | None) -> str | None:
    """Return the scope arg if this is a `revamp <scope>` command in the revamp
    channel, else None. Gated on REVAMP_CHANNEL_ID so the command is inert
    everywhere else (prod channels never trigger the pipeline)."""
    rid = (settings.revamp_channel_id or "").strip()
    if not rid or (channel or "").strip() != rid:
        return None
    m = _REVAMP_CMD_RE.match(text or "")
    return m.group("scope").strip() if m else None


_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _ticket_from_context(text: str, messages: list[dict]) -> str | None:
    m = _TICKET_RE.search(text)
    if m:
        return m.group(1)
    for msg in reversed(messages):
        m = _TICKET_RE.search(msg.get("text") or "")
        if m:
            return m.group(1)
    return None


async def _resolve_active_workspace(
    thread_row: dict | None, text: str, prior_messages: list[dict]
) -> dict | None:
    """Locate a prepared worktree for the ticket in play, resilient to whether
    `active_*` was persisted (older prepare runs didn't record it). Resolves the
    worktree by branch via git, so any on-disk path is found. Returns a dict with
    ticket/worktree/service/github_repo/base/feature_branch, or None."""
    ticket = ((thread_row or {}).get("active_ticket") or "").strip()
    if not ticket:
        ticket = _ticket_from_context(text, prior_messages) or ""
    if not ticket:
        return None
    slug = ((thread_row or {}).get("repo") or "").strip()
    slug = slug or _service_slug_from_registry(text, prior_messages) or ""
    svc = resolve_service_by_github_repo(slug) if slug else None
    if not svc:
        return None
    worktree = await git_int.resolve_existing_worktree(svc, ticket)
    if not worktree:
        return None
    base = ""
    try:
        base = await git_int._resolve_base_branch(svc)
    except Exception:
        log.warning("could not resolve base branch for active workspace")
    return {
        "ticket": ticket,
        "worktree": str(worktree),
        "service": svc["name"],
        "github_repo": (svc.get("github_repo") or "").strip(),
        "base": base,
        "feature_branch": f"feature/{ticket}",
    }


def _workspace_brain_hint(ws: dict) -> str:
    """Tell the brain a worktree is ready so it routes fix/PR work to dev."""
    return (
        "## Workspace đang mở\n"
        f"Thread này đã có worktree sẵn cho ticket `{ws['ticket']}` "
        f"(service `{ws['service']}`, branch `{ws['feature_branch']}`).\n"
        f"- Worktree path (cwd để đọc/sửa/commit): `{ws['worktree']}`\n"
        "Nếu user muốn fix/sửa/commit/push/tạo PR cho ticket này → giao cho `dev`. "
        "Nhớ chuyển nguyên worktree path trên cho dev để nó edit đúng chỗ "
        "(dev tự edit, commit, push, mở PR rồi báo link)."
    )


async def handle_message(
    text: str,
    *,
    thread_ts: str | None,
    channel: str | None,
    user_id: str | None,
    thread_history: list[dict] | None = None,
    slack_client=None,
    placeholder_ts: str | None = None,
) -> str:
    t_start = time.time()
    text, input_truncated = _truncate(text, settings.max_input_chars, label="input")

    # Revamp pipeline: deterministic SCAN→ARCHAEOLOGY→SPEC→Notion, bypasses the
    # brain session. Only fires for `revamp <scope>` in the revamp channel.
    scope = _revamp_scope(text, channel)
    if scope is not None:
        if slack_client is None or not placeholder_ts:
            raise RuntimeError("revamp pipeline requires slack_client + placeholder_ts")
        if thread_ts:
            touch_thread(thread_ts, channel)
        reply = await run_revamp_pipeline(
            scope=scope,
            thread_ts=thread_ts or "",
            channel=channel or "",
            slack_client=slack_client,
            placeholder_ts=placeholder_ts,
        )
        log_run(
            agent="revamp",
            input_text=text,
            output=reply,
            status="ok",
            duration_ms=int((time.time() - t_start) * 1000),
            thread_ts=thread_ts,
            channel=channel,
            user_id=user_id,
        )
        if thread_ts:
            add_message(thread_ts, "assistant", reply)
        return reply

    prior_messages: list[dict] = []
    thread_row: dict | None = None
    if thread_ts:
        touch_thread(thread_ts, channel)
        thread_row = get_thread(thread_ts)
        # Slack thread history (when available) covers non-mention user messages
        # the DB never sees; fall back to DB if Slack fetch returned nothing.
        prior_messages = thread_history or recent_messages(thread_ts, limit=10)

    # Resolve any worktree already prepared for the ticket in play so the brain
    # can route fix/PR work to the dev sub-agent inside it.
    workspace = await _resolve_active_workspace(thread_row, text, prior_messages)
    if workspace and thread_ts:
        fields = {
            "active_ticket": workspace["ticket"],
            "active_worktree": workspace["worktree"],
        }
        if workspace.get("github_repo") and not (thread_row or {}).get("repo"):
            fields["repo"] = workspace["github_repo"]
        update_thread_fields(thread_ts, **fields)

    if slack_client is None or not placeholder_ts:
        raise RuntimeError(
            "SDK brain path requires slack_client + placeholder_ts from the Job "
            "(worker must forward them)"
        )
    brain_pool = _brain_pool_singleton()
    pending_perms = _pending_singleton()
    if brain_pool is None or pending_perms is None:
        raise RuntimeError(
            "SDK brain singletons not initialized "
            "(main.py must call init_sdk_singletons)"
        )

    brain_result = await run_brain_session(
        user_text=text,
        thread_ts=thread_ts or "",
        channel_id=channel or "",
        slack_client=slack_client,
        placeholder_ts=placeholder_ts,
        thread_history=thread_history or [],
        workspace_hint=_workspace_brain_hint(workspace) if workspace else None,
        pool=brain_pool,
        pending=pending_perms,
    )

    # Per-tool runs rows are written by the PostToolUse/PostToolUseFailure hooks
    # (§12.J). Here we log only the brain summary row, carrying the session
    # usage/cost for the observability columns (§12.K).
    log_run(
        agent="brain",
        input_text=text,
        output=brain_result.reply,
        status="error" if brain_result.error else "ok",
        duration_ms=brain_result.duration_ms,
        thread_ts=thread_ts,
        channel=channel,
        user_id=user_id,
        error=brain_result.error,
        usage=brain_result.usage,
        cost_usd=brain_result.cost_usd,
        num_turns=brain_result.num_turns,
    )

    reply_text = brain_result.reply or "(no output)"
    if input_truncated:
        reply_text += (
            f"\n\n⚠️ input quá dài, đã cắt còn {settings.max_input_chars} ký tự"
        )
    if brain_result.error:
        reply_text = f"{reply_text}\n\n⚠️ {brain_result.error}".strip()
    if thread_ts:
        # Durability/fallback for the recent_messages() path when a later Slack
        # history fetch fails; the live session is the source of truth.
        add_message(thread_ts, "assistant", reply_text)
    return _with_footer(
        reply_text,
        brain_result.tool_use_count + 1,
        t_start,
        usage=brain_result.usage,
        cost_usd=brain_result.cost_usd,
    )
