import re

import httpx

from ..config import settings
from .result import ToolResult, classify_exception


# ---------- helpers ----------

# A Jira key (PROJ-123) possibly embedded in a browse URL, Slack-wrapped <url>,
# or surrounding prose. We extract it so callers can pass a pasted link.
_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+", re.IGNORECASE)


def _issue_key(raw: str) -> str:
    """Normalize a user/brain-supplied issue reference to a bare key.

    Accepts a bare ``ABC-123`` or anything containing one (e.g.
    ``https://x.atlassian.net/browse/ABC-123``). Falls back to the stripped
    input when no key is found, so a bad value surfaces a clear Jira NOT_FOUND
    instead of a silent wrong lookup."""
    if not raw:
        return raw
    m = _KEY_RE.search(raw)
    return m.group(0).upper() if m else raw.strip()


def _base() -> str:
    if not settings.jira_base_url:
        raise RuntimeError("JIRA_BASE_URL not configured")
    return settings.jira_base_url.rstrip("/")


def _auth() -> tuple[str, str]:
    if not (settings.jira_email and settings.jira_api_token):
        raise RuntimeError("JIRA_EMAIL / JIRA_API_TOKEN not configured")
    return (settings.jira_email, settings.jira_api_token)


def _project(project: str | None) -> str:
    project = project or settings.jira_default_project
    if not project:
        raise ValueError("project (vd 'KRP')")
    return project


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=30,
        auth=_auth(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )


def _browse_url(key: str) -> str:
    return f"{_base()}/browse/{key}"


def _adf_blocks(text: str) -> list[dict]:
    """Convert plain/markdown-ish text into ADF block nodes.

    A single ``text`` node does not render newlines, so multi-line ticket
    bodies collapse into one wall of prose. This splits on blank lines into
    paragraphs, groups ``- ``/``* `` runs into a bulletList, promotes ``# ``
    lines to headings, and joins remaining lines in a block with hardBreaks —
    enough structure for a business-logic ticket (scenarios / cases) to read
    cleanly without a full Markdown parser."""
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para():
        if not para:
            return
        content: list[dict] = []
        for idx, ln in enumerate(para):
            if idx:
                content.append({"type": "hardBreak"})
            content.append({"type": "text", "text": ln})
        blocks.append({"type": "paragraph", "content": content})
        para.clear()

    def flush_bullets():
        if not bullets:
            return
        blocks.append({
            "type": "bulletList",
            "content": [
                {"type": "listItem",
                 "content": [{"type": "paragraph",
                              "content": [{"type": "text", "text": b}]}]}
                for b in bullets
            ],
        })
        bullets.clear()

    for raw in lines:
        ln = raw.rstrip()
        stripped = ln.lstrip()
        if not stripped:
            flush_para(); flush_bullets()
            continue
        if stripped.startswith(("- ", "* ")):
            flush_para()
            bullets.append(stripped[2:].strip())
            continue
        flush_bullets()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped[level:].strip()
            # An empty heading (bare "#"/"# ") would emit a blank ADF text node,
            # which Jira rejects with 400. Fall back to treating it as prose.
            if heading:
                flush_para()
                blocks.append({
                    "type": "heading",
                    "attrs": {"level": min(max(level, 1), 6)},
                    "content": [{"type": "text", "text": heading}],
                })
                continue
        para.append(stripped)
    flush_para(); flush_bullets()
    if not blocks:
        blocks.append({"type": "paragraph", "content": [{"type": "text", "text": text}]})
    return blocks


def _mention_paragraph(mentions: list[dict]) -> dict | None:
    """A trailing ``cc: @a @b`` paragraph of ADF mention nodes.

    Each entry is ``{"account_id": str, "name"?: str}``. The ``id`` drives the
    Jira notification; ``text`` is the raw-text fallback shown in editors that
    don't hydrate the mention. Entries without an account_id are skipped."""
    nodes: list[dict] = [{"type": "text", "text": "cc: "}]
    added = False
    for m in mentions:
        aid = (m or {}).get("account_id")
        if not aid:
            continue
        name = (m.get("name") or "").strip()
        nodes.append({
            "type": "mention",
            "attrs": {"id": aid, "text": f"@{name}" if name else "@"},
        })
        nodes.append({"type": "text", "text": " "})
        added = True
    return {"type": "paragraph", "content": nodes} if added else None


def _adf(text: str) -> dict:
    return {"type": "doc", "version": 1, "content": _adf_blocks(text)}


def _adf_to_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(part for part in (_adf_to_text(n) for n in node) if part)
    if not isinstance(node, dict):
        return ""

    text = node.get("text") or ""
    content = _adf_to_text(node.get("content"))
    node_type = node.get("type")
    if node_type in {"paragraph", "heading", "blockquote"}:
        return (text + ("\n" if text and content else "") + content).strip()
    if node_type in {"bulletList", "orderedList"}:
        return content
    if node_type == "listItem":
        item = content.strip()
        return f"- {item}" if item else ""
    return (text + content).strip()


def _fmt_issue_line(i: dict) -> str:
    f = i.get("fields", {})
    status = (f.get("status") or {}).get("name", "?")
    summary = f.get("summary", "")
    assignee = (f.get("assignee") or {}).get("displayName") or "unassigned"
    return f"• <{_browse_url(i['key'])}|{i['key']}> · {status} · @{assignee} — {summary}"


async def _search_jql(jql: str, label: str, max_results: int = 20) -> ToolResult:
    async with _client() as c:
        r = await c.get(
            f"{_base()}/rest/api/3/search/jql",
            params={"jql": jql, "maxResults": max_results,
                    "fields": "summary,status,assignee"},
        )
        r.raise_for_status()
        data = r.json()
    issues = data.get("issues", [])
    if not issues:
        return ToolResult.success(f"_{label}: no issues._")
    lines = [f"*{label}* ({len(issues)}):"]
    lines.extend(_fmt_issue_line(i) for i in issues)
    return ToolResult.success("\n".join(lines))


# ---------- named intents (read) ----------

async def list_my_issues(state: str = "open") -> ToolResult:
    if state == "done":
        jql = "assignee = currentUser() AND statusCategory = Done ORDER BY updated DESC"
        label = "Your issues (done)"
    elif state == "all":
        jql = "assignee = currentUser() ORDER BY updated DESC"
        label = "Your issues (all)"
    else:
        jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        label = "Your issues (open)"
    return await _search_jql(jql, label)


async def list_my_in_progress() -> ToolResult:
    jql = 'assignee = currentUser() AND status = "In Progress" ORDER BY updated DESC'
    return await _search_jql(jql, "In progress")


async def list_my_sprint(status: str | None = None) -> ToolResult:
    parts = ["assignee = currentUser()", "sprint in openSprints()"]
    if status:
        parts.append(f'status = "{status}"')
    jql = " AND ".join(parts) + " ORDER BY updated DESC"
    label = f"Current sprint ({status})" if status else "Current sprint"
    return await _search_jql(jql, label)


async def list_project_in_progress(project: str | None = None) -> ToolResult:
    project = _project(project)
    jql = (f'project = {project} AND status = "In Progress" '
           "ORDER BY updated DESC")
    return await _search_jql(jql, f"{project} in progress")


async def get_issue(key: str) -> ToolResult:
    key = _issue_key(key)
    async with _client() as c:
        r = await c.get(
            f"{_base()}/rest/api/3/issue/{key}",
            params={
                "fields": "summary,status,assignee,reporter,priority,issuetype,description"
            },
        )
        r.raise_for_status()
        i = r.json()
    f = i["fields"]
    status = (f.get("status") or {}).get("name", "?")
    assignee = (f.get("assignee") or {}).get("displayName") or "unassigned"
    reporter = (f.get("reporter") or {}).get("displayName") or "?"
    priority = (f.get("priority") or {}).get("name") or "?"
    itype = (f.get("issuetype") or {}).get("name") or "?"
    description = _adf_to_text(f.get("description")).strip()
    if len(description) > 4000:
        description = description[:3900] + "\n…[description truncated]"
    body = (
        f"*<{_browse_url(i['key'])}|{i['key']}>* — {f.get('summary','')}\n"
        f"{itype} · {status} · prio {priority} · @{assignee} (reporter @{reporter})"
    )
    if description:
        body += f"\n\n*Description / specs:*\n{description}"
    return ToolResult.success(body)


# KR team roster — stable role → person data (accountIds resolved live once).
# Lives here as data, not in any prompt, so the brain reasons about *who* to cc
# from the tool result + thread context rather than a hard-coded prompt rule.
KR_TEAM_ROSTER: list[dict] = [
    {"role": "Tech Lead / EM", "name": "John Dinh",           "account_id": "635786eb13f37118d72633d6"},
    {"role": "PM",             "name": "Hyeyoung Hailey Sim",  "account_id": "712020:3afe9784-6470-4473-83a6-27a2c24fec46"},
    {"role": "PM",             "name": "winter_jukyung.oh",    "account_id": "62207ae2a687c5006a5ed4ab"},
    {"role": "BE senior",      "name": "Danny Nguyen",         "account_id": "62eb49a03cc20c06c8af2092"},
    {"role": "QA",             "name": "Emily Pham",           "account_id": "627dfe5f9311100068a17ea2"},
    {"role": "Reporter",       "name": "Tyron",                "account_id": "629819fa1c69c7006ac55162"},
]


async def list_team() -> ToolResult:
    """The KR team roster (role → name → accountId) for @-mentioning by role.

    Use to resolve who a role refers to ("cc PM / QA / tech lead") before
    passing `mentions` to jira_create_issue. For anyone not listed, fall back
    to search_users."""
    lines = ["*KR team roster:*"]
    lines += [f"• {m['role']}: {m['name']} · `{m['account_id']}`" for m in KR_TEAM_ROSTER]
    return ToolResult.success("\n".join(lines))


async def search_users(query: str, max_results: int = 10) -> ToolResult:
    """Search Jira users by name or email → accountId (for @-mentions / assign).

    Returns a compact list the brain can map name/role → accountId when a
    person is not in the built-in roster."""
    async with _client() as c:
        r = await c.get(f"{_base()}/rest/api/3/user/search",
                        params={"query": query, "maxResults": max(1, min(max_results, 50))})
        r.raise_for_status()
        users = r.json() or []
    people = [u for u in users if u.get("accountType") == "atlassian"] or users
    if not people:
        return ToolResult.failure("NOT_FOUND", f"No Jira user matching `{query}`.")
    lines = [f"*Jira users matching `{query}`* ({len(people)}):"]
    for u in people:
        name = u.get("displayName") or "?"
        email = u.get("emailAddress") or "—"
        lines.append(f"• {name} · {email} · `{u.get('accountId')}`")
    return ToolResult.success("\n".join(lines))


async def search_jql(jql: str, max_results: int = 20, kind: str = "Results") -> ToolResult:
    """Escape hatch — raw JQL. Not exposed in the default prompt."""
    return await _search_jql(jql, kind, max_results)


async def get_active_sprint(board_id: int | None = None) -> dict:
    """Return {'id': int, 'name': str, 'number': int|None} for the active sprint.

    Raises RuntimeError if no board configured / no active sprint found.
    """
    bid = board_id or settings.jira_board_id
    if not bid:
        raise RuntimeError("JIRA_BOARD_ID not configured")
    async with _client() as c:
        r = await c.get(
            f"{_base()}/rest/agile/1.0/board/{bid}/sprint",
            params={"state": "active"},
        )
        r.raise_for_status()
        sprints = r.json().get("values", [])
    if not sprints:
        raise RuntimeError(f"No active sprint on board {bid}")
    s = sprints[0]
    name = s.get("name", "")
    # Extract trailing integer from sprint name (e.g. "DAPro-2.126" -> 126, "Sprint 126" -> 126)
    import re as _re
    m = _re.search(r"(\d+)(?!.*\d)", name)
    return {"id": s["id"], "name": name, "number": int(m.group(1)) if m else None}


# ---------- named intents (write) ----------

async def create_issue(summary: str, description: str = "",
                       project: str | None = None,
                       issue_type: str = "Task",
                       mentions: list[dict] | None = None) -> ToolResult:
    project = _project(project)
    doc = {"type": "doc", "version": 1,
           "content": _adf_blocks(description) if description else []}
    if mentions:
        cc = _mention_paragraph(mentions)
        if cc:
            doc["content"].append(cc)
    payload = {
        "fields": {
            "project": {"key": project},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
    }
    if doc["content"]:
        payload["fields"]["description"] = doc
    async with _client() as c:
        r = await c.post(f"{_base()}/rest/api/3/issue", json=payload)
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(f"✅ Created <{_browse_url(d['key'])}|{d['key']}>: {summary}")


async def list_transitions(key: str) -> ToolResult:
    async with _client() as c:
        r = await c.get(f"{_base()}/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        ts = r.json().get("transitions", [])
    if not ts:
        return ToolResult.success(f"_No transitions available for {key}._")
    lines = [f"*Transitions for <{_browse_url(key)}|{key}>*:"]
    for t in ts:
        lines.append(f"• `{t['name']}` → {t['to']['name']}")
    return ToolResult.success("\n".join(lines))


async def transition_issue(key: str, target_status: str) -> ToolResult:
    async with _client() as c:
        r = await c.get(f"{_base()}/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        ts = r.json().get("transitions", [])
        match = next((t for t in ts if t["name"].lower() == target_status.lower()
                      or t["to"]["name"].lower() == target_status.lower()), None)
        if not match:
            names = ", ".join(t["name"] for t in ts)
            return ToolResult.failure(
                "VALIDATION",
                f"No transition `{target_status}` for {key}. Available: {names}",
            )
        r = await c.post(
            f"{_base()}/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": match["id"]}},
        )
        r.raise_for_status()
    return ToolResult.success(f"✅ <{_browse_url(key)}|{key}> → *{match['to']['name']}*")


async def assign_issue(key: str, assignee: str | None = None) -> ToolResult:
    """Assign a Jira issue (Jira Cloud assigns by accountId).

    - assignee empty / "me" / "self" (or Vietnamese equivalents) → current API-token user (``/myself``).
    - otherwise → resolve by email or display name via user search (first match).
    """
    want = (assignee or "").strip().lower()
    async with _client() as c:
        if want in ("", "me", "tôi", "toi", "self", "mình", "minh", "myself"):
            r = await c.get(f"{_base()}/rest/api/3/myself")
            r.raise_for_status()
            u = r.json()
        else:
            r = await c.get(f"{_base()}/rest/api/3/user/search", params={"query": assignee})
            r.raise_for_status()
            users = r.json() or []
            if not users:
                return ToolResult.failure(
                    "NOT_FOUND", f"No Jira user matching `{assignee}`."
                )
            u = users[0]
        account_id = u.get("accountId")
        who = u.get("displayName") or assignee or "you"
        r = await c.put(
            f"{_base()}/rest/api/3/issue/{key}/assignee",
            json={"accountId": account_id},
        )
        if r.status_code not in (200, 204):
            try:
                detail = (r.json() or {}).get("errorMessages") or r.text
            except Exception:
                detail = r.text
            return ToolResult.failure(
                "VALIDATION", f"Jira rejected assign {key}: {str(detail)[:200]}"
            )
    return ToolResult.success(f"✅ Assigned <{_browse_url(key)}|{key}> to *{who}*")


async def comment_issue(key: str, body: str) -> ToolResult:
    async with _client() as c:
        r = await c.post(
            f"{_base()}/rest/api/3/issue/{key}/comment",
            json={"body": _adf(body)},
        )
        r.raise_for_status()
    return ToolResult.success(f"✅ Commented on <{_browse_url(key)}|{key}>")


async def get_comments(key: str, limit: int = 5) -> ToolResult:
    """Last `limit` (default 5) comments on an issue, oldest→newest for reading.

    Accepts a key or a browse URL. The Jira `comment` field is not fetched by
    `get_issue` (keeps that response lean), so this is the dedicated read path."""
    key = _issue_key(key)
    limit = max(1, min(limit, 20))
    async with _client() as c:
        r = await c.get(
            f"{_base()}/rest/api/3/issue/{key}/comment",
            params={"orderBy": "-created", "maxResults": limit},
        )
        r.raise_for_status()
        data = r.json()
    comments = data.get("comments") or []
    if not comments:
        return ToolResult.success(f"*<{_browse_url(key)}|{key}>* — no comments yet.")
    total = data.get("total", len(comments))
    # orderBy=-created returns newest-first; reverse so the brain reads in order.
    shown = list(reversed(comments))
    header = f"*<{_browse_url(key)}|{key}>* — {len(shown)} most recent comments"
    if total > len(shown):
        header += f" (total {total})"
    lines = [header + ":"]
    for cm in shown:
        author = (cm.get("author") or {}).get("displayName") or "?"
        when = (cm.get("created") or "")[:10]
        text = _adf_to_text(cm.get("body")).strip()
        if len(text) > 1500:
            text = text[:1450] + "\n…[comment truncated]"
        lines.append(f"\n• *{author}* ({when}):\n{text}")
    return ToolResult.success("\n".join(lines))


# ---------- dispatch ----------

ACTION_HANDLERS = {
    "jira.list_my_issues":            lambda p: list_my_issues(p.get("state", "open")),
    "jira.list_my_in_progress":       lambda p: list_my_in_progress(),
    "jira.list_my_sprint":            lambda p: list_my_sprint(p.get("status")),
    "jira.list_project_in_progress":  lambda p: list_project_in_progress(p.get("project")),
    "jira.get_issue":                 lambda p: get_issue(p["key"]),
    "jira.get_comments":              lambda p: get_comments(p["key"], p.get("limit", 5)),
    "jira.search":                    lambda p: search_jql(p["jql"], p.get("max_results", 20),
                                                           p.get("kind", "Results")),
    "jira.list_team":                 lambda p: list_team(),
    "jira.search_users":              lambda p: search_users(p["query"], p.get("max_results", 10)),
    "jira.create_issue":              lambda p: create_issue(p["summary"], p.get("description", ""),
                                                             p.get("project"), p.get("issue_type", "Task"),
                                                             p.get("mentions")),
    "jira.comment_issue":             lambda p: comment_issue(p["key"], p["body"]),
    "jira.assign_issue":              lambda p: assign_issue(p["key"], p.get("assignee")),
    "jira.list_transitions":          lambda p: list_transitions(p["key"]),
    "jira.transition_issue":          lambda p: transition_issue(p["key"], p["target_status"]),
}


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action_type}`")
    try:
        return await handler(payload)
    except Exception as e:
        return classify_exception(e, service="Jira")
