import httpx

from ..config import settings
from .result import ToolResult, classify_exception

API = "https://api.github.com"


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN not configured")
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo(repo: str | None) -> str:
    repo = repo or settings.github_default_repo
    if not repo or "/" not in repo:
        raise ValueError("repo must be 'owner/name'")
    return repo


def _me() -> str:
    if not settings.github_username:
        raise ValueError("GITHUB_USERNAME not configured (needed for 'my' queries)")
    return settings.github_username


# ---------- write tools ----------

async def create_issue(title: str, body: str, repo: str | None = None) -> str:
    repo = _repo(repo)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/issues",
            headers=_headers(),
            json={"title": title, "body": body},
        )
        r.raise_for_status()
        d = r.json()
        return f"✅ Tạo issue #{d['number']} trong `{repo}`: <{d['html_url']}|{d['title']}>"


async def comment_pr(pr: int, body: str, repo: str | None = None) -> str:
    repo = _repo(repo)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/issues/{pr}/comments",
            headers=_headers(),
            json={"body": body},
        )
        r.raise_for_status()
        return f"✅ Đã comment vào PR #{pr} `{repo}`: <{r.json()['html_url']}|xem comment>"


async def approve_pr(
    pr: int, repo: str | None = None, body: str = "", confirmed: bool = False
) -> ToolResult:
    repo = _repo(repo)
    if not confirmed:
        question = f"Ông xác nhận **approve review** PR #{pr} `{repo}`?"
        if body:
            question += f"\n> {body}"
        res = ToolResult.failure("NEEDS_CONFIRMATION", question)
        res.data = {
            "action_type": "github.approve_pr",
            "payload": {"repo": repo, "pr": pr, "body": body, "confirmed": True},
        }
        return res
    review_body: dict = {"event": "APPROVE"}
    if body:
        review_body["body"] = body
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/pulls/{pr}/reviews",
            headers=_headers(),
            json=review_body,
        )
        if r.status_code == 422:
            # Most common: author trying to approve own PR, or PR is closed.
            detail = (r.json() or {}).get("message") or "không approve được"
            return ToolResult.failure(
                "VALIDATION", f"GitHub từ chối approve PR #{pr} `{repo}`: {detail}"
            )
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(
        f"✅ Đã approve review PR #{pr} `{repo}`: <{d.get('html_url') or ''}|xem review>"
    )


async def create_pr(
    title: str,
    head: str,
    base: str,
    body: str = "",
    repo: str | None = None,
    draft: bool = False,
    confirmed: bool = False,
) -> ToolResult:
    repo = _repo(repo)
    if not title.strip():
        return ToolResult.failure("VALIDATION", "PR title không được rỗng.")
    if not head.strip() or not base.strip():
        return ToolResult.failure("VALIDATION", "head/base branch không được rỗng.")

    if not confirmed:
        draft_label = "draft " if draft else ""
        question = (
            f"Tạo {draft_label}PR trong `{repo}`: `{head}` → `{base}`\n"
            f"Title: {title}\n"
            f"(reply: ok / không)"
        )
        res = ToolResult.failure("NEEDS_CONFIRMATION", question)
        res.data = {
            "action_type": "github.create_pr",
            "payload": {"repo": repo, "title": title, "head": head, "base": base,
                        "body": body, "draft": draft, "confirmed": True},
        }
        return res

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/pulls",
            headers=_headers(),
            json={"title": title, "head": head, "base": base,
                  "body": body, "draft": draft},
        )
        if r.status_code == 422:
            data = r.json() or {}
            msg = data.get("message") or ""
            errors_text = " ".join(
                str(e.get("message", "")) for e in (data.get("errors") or [])
            )
            looks_existing = "already exists" in (msg + errors_text).lower()
            if looks_existing:
                owner = repo.split("/", 1)[0]
                async with httpx.AsyncClient(timeout=30) as c2:
                    r2 = await c2.get(
                        f"{API}/repos/{repo}/pulls",
                        headers=_headers(),
                        params={"head": f"{owner}:{head}", "state": "open"},
                    )
                    r2.raise_for_status()
                    existing = r2.json()
                if existing:
                    p = existing[0]
                    return ToolResult.success(
                        f"ℹ️ PR đã tồn tại: <{p['html_url']}|#{p['number']} {p['title']}>"
                    )
            detail = msg or errors_text or "tạo PR bị từ chối"
            return ToolResult.failure(
                "VALIDATION", f"GitHub từ chối tạo PR `{repo}`: {detail}"
            )
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(
        f"✅ Tạo PR #{d['number']} `{repo}`: <{d['html_url']}|{d['title']}>"
    )


_MERGE_METHODS = {"squash", "merge", "rebase"}
_MERGE_OK_STATES = {"clean", "unstable"}  # 'unstable' = optional checks failing, allowed


async def merge_pr(
    pr: int,
    repo: str | None = None,
    method: str = "squash",
    commit_title: str = "",
    commit_message: str = "",
    confirmed: bool = False,
) -> ToolResult:
    repo = _repo(repo)
    if method not in _MERGE_METHODS:
        return ToolResult.failure(
            "VALIDATION", f"merge method `{method}` không hợp lệ (squash/merge/rebase)"
        )

    # Always re-check mergeability — even on resume, state may have changed.
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/pulls/{pr}", headers=_headers())
        r.raise_for_status()
        p = r.json()

    if p.get("merged"):
        return ToolResult.failure(
            "VALIDATION", f"PR #{pr} `{repo}` đã merge rồi (sha {p.get('merge_commit_sha')})."
        )
    if p.get("state") != "open":
        return ToolResult.failure(
            "VALIDATION", f"PR #{pr} `{repo}` đang ở state `{p.get('state')}`, không merge được."
        )
    if p.get("draft"):
        return ToolResult.failure(
            "VALIDATION", f"PR #{pr} `{repo}` đang là draft."
        )

    state = p.get("mergeable_state") or "unknown"
    if state not in _MERGE_OK_STATES:
        reasons = {
            "dirty": "có conflict với base branch",
            "blocked": "bị block (thiếu required approvals hoặc required checks fail)",
            "behind": "branch đang behind base, cần update branch",
            "draft": "PR đang draft",
            "unknown": "GitHub chưa tính xong mergeable state, thử lại sau xíu",
            "has_hooks": "có hook chặn (cần admin merge)",
        }
        why = reasons.get(state, f"mergeable_state = `{state}`")
        return ToolResult.failure(
            "VALIDATION", f"⛔ PR #{pr} `{repo}` chưa merge được: {why}."
        )

    if not confirmed:
        head = p.get("head", {}).get("ref", "?")
        base = p.get("base", {}).get("ref", "?")
        question = (
            f"Ông xác nhận **merge** PR #{pr} `{repo}` "
            f"(`{head}` → `{base}`, method `{method}`)?"
        )
        res = ToolResult.failure("NEEDS_CONFIRMATION", question)
        res.data = {
            "action_type": "github.merge_pr",
            "payload": {
                "repo": repo,
                "pr": pr,
                "method": method,
                "commit_title": commit_title,
                "commit_message": commit_message,
                "confirmed": True,
            },
        }
        return res

    merge_body: dict = {"merge_method": method}
    if commit_title:
        merge_body["commit_title"] = commit_title
    if commit_message:
        merge_body["commit_message"] = commit_message
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.put(
            f"{API}/repos/{repo}/pulls/{pr}/merge",
            headers=_headers(),
            json=merge_body,
        )
        if r.status_code in (405, 409):
            detail = (r.json() or {}).get("message") or "merge bị từ chối"
            return ToolResult.failure(
                "VALIDATION", f"GitHub từ chối merge PR #{pr} `{repo}`: {detail}"
            )
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(
        f"✅ Đã merge PR #{pr} `{repo}` ({method}). SHA `{d.get('sha', '?')[:7]}`."
    )


# ---------- read tools ----------

async def list_my_prs(state: str = "open") -> str:
    me = _me()
    q = f"is:pr is:{state} (author:{me} OR assignee:{me} OR review-requested:{me})"
    return await search(q, kind="my PRs")


async def list_prs(repo: str, state: str = "open", author: str | None = None) -> str:
    repo = _repo(repo)
    params = {"state": state, "per_page": 30}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/pulls", headers=_headers(), params=params)
        r.raise_for_status()
        items = r.json()
    if author:
        items = [p for p in items if p["user"]["login"] == author]
    if not items:
        return f"_Không có PR `{state}` nào trong `{repo}`._"
    lines = [f"*PR `{state}` trong `{repo}`* ({len(items)}):"]
    for p in items[:20]:
        lines.append(
            f"• #{p['number']} <{p['html_url']}|{p['title']}> "
            f"— @{p['user']['login']} · {p.get('draft') and '📝 draft' or '🟢'}"
        )
    return "\n".join(lines)


async def list_issues(repo: str, state: str = "open", assignee: str | None = None,
                      label: str | None = None) -> str:
    repo = _repo(repo)
    params: dict = {"state": state, "per_page": 30}
    if assignee:
        params["assignee"] = assignee
    if label:
        params["labels"] = label
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/issues", headers=_headers(), params=params)
        r.raise_for_status()
        # /issues includes PRs — filter them out
        items = [i for i in r.json() if "pull_request" not in i]
    if not items:
        return f"_Không có issue `{state}` nào trong `{repo}`._"
    lines = [f"*Issue `{state}` trong `{repo}`* ({len(items)}):"]
    for i in items[:20]:
        labels = ", ".join(l["name"] for l in i.get("labels", []))
        suffix = f" · 🏷 {labels}" if labels else ""
        lines.append(f"• #{i['number']} <{i['html_url']}|{i['title']}> — @{i['user']['login']}{suffix}")
    return "\n".join(lines)


async def list_notifications(all: bool = False) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{API}/notifications",
            headers=_headers(),
            params={"all": "true" if all else "false", "per_page": 30},
        )
        r.raise_for_status()
        items = r.json()
    if not items:
        return "_Inbox GitHub sạch sẽ 🎉_"
    lines = [f"*Notifications* ({len(items)} {'all' if all else 'unread'}):"]
    for n in items[:20]:
        repo = n["repository"]["full_name"]
        subj = n["subject"]
        typ = subj["type"]
        # subject.url is API URL; convert to html_url best-effort
        api_url = subj.get("url") or ""
        html_url = api_url.replace("https://api.github.com/repos/", "https://github.com/") \
            .replace("/pulls/", "/pull/")
        lines.append(f"• [{typ}] `{repo}` — <{html_url}|{subj['title']}> · {n['reason']}")
    return "\n".join(lines)


async def search(query: str, kind: str = "search") -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{API}/search/issues",
            headers=_headers(),
            params={"q": query, "per_page": 30},
        )
        r.raise_for_status()
        data = r.json()
    items = data.get("items", [])
    total = data.get("total_count", 0)
    if not items:
        return f"_Không có kết quả cho `{query}`._"
    lines = [f"*{kind}* — `{query}` ({total} tổng, hiển thị {min(len(items), 20)}):"]
    for i in items[:20]:
        is_pr = "pull_request" in i
        kind_icon = "🔀" if is_pr else "🐛"
        repo = "/".join(i["repository_url"].split("/")[-2:])
        lines.append(
            f"• {kind_icon} `{repo}` #{i['number']} <{i['html_url']}|{i['title']}> "
            f"— @{i['user']['login']} · {i['state']}"
        )
    return "\n".join(lines)


async def get_pr(pr: int, repo: str | None = None) -> str:
    repo = _repo(repo)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/pulls/{pr}", headers=_headers())
        r.raise_for_status()
        p = r.json()
    body = (p.get("body") or "")[:500]
    return (
        f"*PR #{p['number']} `{repo}`* — <{p['html_url']}|{p['title']}>\n"
        f"@{p['user']['login']} · {p['state']} · +{p['additions']}/-{p['deletions']} · "
        f"{p['changed_files']} files · base `{p['base']['ref']}` ← `{p['head']['ref']}`\n"
        f"```{body}```"
    )


async def get_pr_diff(pr: int, repo: str | None = None, max_chars: int = 20000) -> str:
    repo = _repo(repo)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{API}/repos/{repo}/pulls/{pr}",
            headers=_headers(accept="application/vnd.github.v3.diff"),
        )
        r.raise_for_status()
        diff = r.text
    truncated = len(diff) > max_chars
    if truncated:
        diff = diff[:max_chars] + f"\n... [truncated, total {len(r.text)} chars]"
    return f"*PR #{pr} `{repo}` diff*:\n```\n{diff}\n```"


# ---------- dispatch ----------

ACTION_HANDLERS = {
    "github.create_issue":     lambda p: create_issue(p["title"], p["body"], p.get("repo")),
    "github.create_pr":        lambda p: create_pr(p["title"], p["head"], p["base"],
                                                   p.get("body", ""), p.get("repo"),
                                                   bool(p.get("draft", False)),
                                                   bool(p.get("confirmed", False))),
    "github.comment_pr":       lambda p: comment_pr(p["pr"], p["body"], p.get("repo")),
    "github.approve_pr":       lambda p: approve_pr(p["pr"], p.get("repo"), p.get("body", ""),
                                                    bool(p.get("confirmed", False))),
    "github.merge_pr":         lambda p: merge_pr(p["pr"], p.get("repo"),
                                                  p.get("method", "squash"),
                                                  p.get("commit_title", ""),
                                                  p.get("commit_message", ""),
                                                  bool(p.get("confirmed", False))),
    "github.list_my_prs":      lambda p: list_my_prs(p.get("state", "open")),
    "github.list_prs":         lambda p: list_prs(p["repo"], p.get("state", "open"), p.get("author")),
    "github.list_issues":      lambda p: list_issues(p["repo"], p.get("state", "open"),
                                                     p.get("assignee"), p.get("label")),
    "github.list_notifications": lambda p: list_notifications(bool(p.get("all", False))),
    "github.search":           lambda p: search(p["query"], p.get("kind", "search")),
    "github.get_pr":           lambda p: get_pr(p["pr"], p.get("repo")),
    "github.get_pr_diff":      lambda p: get_pr_diff(p["pr"], p.get("repo"),
                                                     p.get("max_chars", 20000)),
}


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action_type}`")
    try:
        result = await handler(payload)
        if isinstance(result, ToolResult):
            return result
        return ToolResult.success(result)
    except Exception as e:
        return classify_exception(e, service="GitHub")
