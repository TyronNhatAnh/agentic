import httpx

from ..config import settings
from .result import ToolResult, classify_exception


# ---------- helpers ----------

def _base() -> str:
    if not settings.jira_base_url:
        raise RuntimeError("JIRA_BASE_URL chưa cấu hình")
    return settings.jira_base_url.rstrip("/")


def _auth() -> tuple[str, str]:
    if not (settings.jira_email and settings.jira_api_token):
        raise RuntimeError("JIRA_EMAIL / JIRA_API_TOKEN chưa cấu hình")
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


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


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
        return ToolResult.success(f"_{label}: không có issue nào._")
    lines = [f"*{label}* ({len(issues)}):"]
    lines.extend(_fmt_issue_line(i) for i in issues)
    return ToolResult.success("\n".join(lines))


# ---------- named intents (read) ----------

async def list_my_issues(state: str = "open") -> ToolResult:
    if state == "done":
        jql = "assignee = currentUser() AND statusCategory = Done ORDER BY updated DESC"
        label = "Issue của bạn (đã done)"
    elif state == "all":
        jql = "assignee = currentUser() ORDER BY updated DESC"
        label = "Issue của bạn (tất cả)"
    else:
        jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        label = "Issue của bạn (đang mở)"
    return await _search_jql(jql, label)


async def list_my_in_progress() -> ToolResult:
    jql = 'assignee = currentUser() AND status = "In Progress" ORDER BY updated DESC'
    return await _search_jql(jql, "Đang làm")


async def list_my_sprint(status: str | None = None) -> ToolResult:
    parts = ["assignee = currentUser()", "sprint in openSprints()"]
    if status:
        parts.append(f'status = "{status}"')
    jql = " AND ".join(parts) + " ORDER BY updated DESC"
    label = f"Sprint hiện tại ({status})" if status else "Sprint hiện tại"
    return await _search_jql(jql, label)


async def list_project_in_progress(project: str | None = None) -> ToolResult:
    project = _project(project)
    jql = (f'project = {project} AND status = "In Progress" '
           "ORDER BY updated DESC")
    return await _search_jql(jql, f"{project} đang làm")


async def get_issue(key: str) -> ToolResult:
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
        description = description[:3900] + "\n…[description cắt bớt]"
    body = (
        f"*<{_browse_url(i['key'])}|{i['key']}>* — {f.get('summary','')}\n"
        f"{itype} · {status} · prio {priority} · @{assignee} (reporter @{reporter})"
    )
    if description:
        body += f"\n\n*Description / specs:*\n{description}"
    return ToolResult.success(body)


async def search_jql(jql: str, max_results: int = 20, kind: str = "Kết quả") -> ToolResult:
    """Escape hatch — raw JQL. Không expose trong prompt mặc định."""
    return await _search_jql(jql, kind, max_results)


async def get_active_sprint(board_id: int | None = None) -> dict:
    """Return {'id': int, 'name': str, 'number': int|None} for the active sprint.

    Raises RuntimeError if no board configured / no active sprint found.
    """
    bid = board_id or settings.jira_board_id
    if not bid:
        raise RuntimeError("JIRA_BOARD_ID chưa cấu hình")
    async with _client() as c:
        r = await c.get(
            f"{_base()}/rest/agile/1.0/board/{bid}/sprint",
            params={"state": "active"},
        )
        r.raise_for_status()
        sprints = r.json().get("values", [])
    if not sprints:
        raise RuntimeError(f"Không có active sprint trên board {bid}")
    s = sprints[0]
    name = s.get("name", "")
    # Extract trailing integer from sprint name (e.g. "DAPro-2.126" -> 126, "Sprint 126" -> 126)
    import re as _re
    m = _re.search(r"(\d+)(?!.*\d)", name)
    return {"id": s["id"], "name": name, "number": int(m.group(1)) if m else None}


# ---------- named intents (write) ----------

async def create_issue(summary: str, description: str = "",
                       project: str | None = None,
                       issue_type: str = "Task") -> ToolResult:
    project = _project(project)
    payload = {
        "fields": {
            "project": {"key": project},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
    }
    if description:
        payload["fields"]["description"] = _adf(description)
    async with _client() as c:
        r = await c.post(f"{_base()}/rest/api/3/issue", json=payload)
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(f"✅ Tạo <{_browse_url(d['key'])}|{d['key']}>: {summary}")


async def list_transitions(key: str) -> ToolResult:
    async with _client() as c:
        r = await c.get(f"{_base()}/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        ts = r.json().get("transitions", [])
    if not ts:
        return ToolResult.success(f"_Không có transition khả dụng cho {key}._")
    lines = [f"*Transitions cho <{_browse_url(key)}|{key}>*:"]
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
                f"Không có transition `{target_status}` cho {key}. Có: {names}",
            )
        r = await c.post(
            f"{_base()}/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": match["id"]}},
        )
        r.raise_for_status()
    return ToolResult.success(f"✅ <{_browse_url(key)}|{key}> → *{match['to']['name']}*")


async def comment_issue(key: str, body: str) -> ToolResult:
    async with _client() as c:
        r = await c.post(
            f"{_base()}/rest/api/3/issue/{key}/comment",
            json={"body": _adf(body)},
        )
        r.raise_for_status()
    return ToolResult.success(f"✅ Đã comment <{_browse_url(key)}|{key}>")


# ---------- dispatch ----------

ACTION_HANDLERS = {
    "jira.list_my_issues":            lambda p: list_my_issues(p.get("state", "open")),
    "jira.list_my_in_progress":       lambda p: list_my_in_progress(),
    "jira.list_my_sprint":            lambda p: list_my_sprint(p.get("status")),
    "jira.list_project_in_progress":  lambda p: list_project_in_progress(p.get("project")),
    "jira.get_issue":                 lambda p: get_issue(p["key"]),
    "jira.search":                    lambda p: search_jql(p["jql"], p.get("max_results", 20),
                                                           p.get("kind", "Kết quả")),
    "jira.create_issue":              lambda p: create_issue(p["summary"], p.get("description", ""),
                                                             p.get("project"), p.get("issue_type", "Task")),
    "jira.comment_issue":             lambda p: comment_issue(p["key"], p["body"]),
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
