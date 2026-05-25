import httpx

from ..config import settings


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
        raise ValueError("project key required (e.g. KRP)")
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


def _fmt_issue_line(i: dict) -> str:
    f = i.get("fields", {})
    status = (f.get("status") or {}).get("name", "?")
    summary = f.get("summary", "")
    assignee = (f.get("assignee") or {}).get("displayName") or "unassigned"
    return f"• <{_browse_url(i['key'])}|{i['key']}> · {status} · @{assignee} — {summary}"


# ---------- read ----------

async def search_jql(jql: str, max_results: int = 20, kind: str = "search") -> str:
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
        return f"_Không có issue nào cho `{jql}`._"
    lines = [f"*Jira {kind}* — `{jql}` ({len(issues)}):"]
    lines.extend(_fmt_issue_line(i) for i in issues)
    return "\n".join(lines)


async def list_my_issues(state: str = "open") -> str:
    if state == "open":
        jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
    elif state == "done":
        jql = "assignee = currentUser() AND statusCategory = Done ORDER BY updated DESC"
    else:
        jql = "assignee = currentUser() ORDER BY updated DESC"
    return await search_jql(jql, kind=f"my issues ({state})")


async def list_project_issues(project: str | None = None, state: str = "open",
                              assignee: str | None = None) -> str:
    project = _project(project)
    parts = [f"project = {project}"]
    if state == "open":
        parts.append("statusCategory != Done")
    elif state == "done":
        parts.append("statusCategory = Done")
    if assignee:
        parts.append(f'assignee = "{assignee}"' if assignee != "me" else "assignee = currentUser()")
    jql = " AND ".join(parts) + " ORDER BY updated DESC"
    return await search_jql(jql, kind=f"{project} issues")


async def get_issue(key: str) -> str:
    async with _client() as c:
        r = await c.get(
            f"{_base()}/rest/api/3/issue/{key}",
            params={"fields": "summary,status,assignee,reporter,priority,issuetype,description"},
        )
        r.raise_for_status()
        i = r.json()
    f = i["fields"]
    status = (f.get("status") or {}).get("name", "?")
    assignee = (f.get("assignee") or {}).get("displayName") or "unassigned"
    reporter = (f.get("reporter") or {}).get("displayName") or "?"
    priority = (f.get("priority") or {}).get("name") or "?"
    itype = (f.get("issuetype") or {}).get("name") or "?"
    return (
        f"*<{_browse_url(i['key'])}|{i['key']}>* — {f.get('summary','')}\n"
        f"{itype} · {status} · prio {priority} · @{assignee} (reporter @{reporter})"
    )


# ---------- write ----------

async def create_issue(summary: str, description: str = "",
                       project: str | None = None,
                       issue_type: str = "Task") -> str:
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
        if r.status_code >= 400:
            return f"❌ Tạo Jira thất bại ({r.status_code}): {r.text[:300]}"
        d = r.json()
    return f"✅ Tạo <{_browse_url(d['key'])}|{d['key']}>: {summary}"


async def list_transitions(key: str) -> str:
    async with _client() as c:
        r = await c.get(f"{_base()}/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        ts = r.json().get("transitions", [])
    if not ts:
        return f"_Không có transition khả dụng cho {key}._"
    lines = [f"*Transitions cho <{_browse_url(key)}|{key}>*:"]
    for t in ts:
        lines.append(f"• `{t['name']}` → {t['to']['name']}")
    return "\n".join(lines)


async def transition_issue(key: str, target_status: str) -> str:
    async with _client() as c:
        r = await c.get(f"{_base()}/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        ts = r.json().get("transitions", [])
        match = next((t for t in ts if t["name"].lower() == target_status.lower()
                      or t["to"]["name"].lower() == target_status.lower()), None)
        if not match:
            names = ", ".join(t["name"] for t in ts)
            return f"❌ Không có transition `{target_status}` cho {key}. Có: {names}"
        r = await c.post(
            f"{_base()}/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": match["id"]}},
        )
        if r.status_code >= 400:
            return f"❌ Transition thất bại ({r.status_code}): {r.text[:300]}"
    return f"✅ <{_browse_url(key)}|{key}> → *{match['to']['name']}*"


async def comment_issue(key: str, body: str) -> str:
    async with _client() as c:
        r = await c.post(
            f"{_base()}/rest/api/3/issue/{key}/comment",
            json={"body": _adf(body)},
        )
        if r.status_code >= 400:
            return f"❌ Comment thất bại ({r.status_code}): {r.text[:300]}"
    return f"✅ Đã comment <{_browse_url(key)}|{key}>"


# ---------- dispatch ----------

ACTION_HANDLERS = {
    "jira.list_my_issues":      lambda p: list_my_issues(p.get("state", "open")),
    "jira.list_project_issues": lambda p: list_project_issues(p.get("project"),
                                                              p.get("state", "open"),
                                                              p.get("assignee")),
    "jira.get_issue":           lambda p: get_issue(p["key"]),
    "jira.search":              lambda p: search_jql(p["jql"], p.get("max_results", 20),
                                                     p.get("kind", "search")),
    "jira.create_issue":        lambda p: create_issue(p["summary"], p.get("description", ""),
                                                       p.get("project"), p.get("issue_type", "Task")),
    "jira.comment_issue":       lambda p: comment_issue(p["key"], p["body"]),
    "jira.list_transitions":    lambda p: list_transitions(p["key"]),
    "jira.transition_issue":    lambda p: transition_issue(p["key"], p["target_status"]),
}


async def execute_action(action_type: str, payload: dict) -> str:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        raise ValueError(f"unknown action: {action_type}")
    return await handler(payload)
