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
        return f"✅ Created issue #{d['number']} in `{repo}`: <{d['html_url']}|{d['title']}>"


async def comment_pr(pr: int, body: str, repo: str | None = None) -> str:
    repo = _repo(repo)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/issues/{pr}/comments",
            headers=_headers(),
            json={"body": body},
        )
        r.raise_for_status()
        return f"✅ Commented on PR #{pr} `{repo}`: <{r.json()['html_url']}|view comment>"


async def add_assignees(
    pr: int, assignees: list[str], repo: str | None = None
) -> ToolResult:
    """Assign users to a PR/issue (PR numbers share the issues API). GitHub
    SILENTLY drops assignees that lack repo access (no error) — we diff requested
    vs actual so the brain reports a real outcome instead of guessing."""
    repo = _repo(repo)
    wanted = [a.lstrip("@").strip() for a in (assignees or []) if a and a.strip()]
    if not wanted:
        return ToolResult.failure("VALIDATION", "Need at least 1 assignee.")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/issues/{pr}/assignees",
            headers=_headers(),
            json={"assignees": wanted},
        )
        if r.status_code == 404:
            return ToolResult.failure(
                "NOT_FOUND", f"PR/issue #{pr} not found in `{repo}`."
            )
        r.raise_for_status()
        got = {a.get("login", "").lower() for a in (r.json().get("assignees") or [])}
    added = [w for w in wanted if w.lower() in got]
    missed = [w for w in wanted if w.lower() not in got]
    if not added:
        return ToolResult.failure(
            "VALIDATION",
            f"Could not assign {', '.join('@'+w for w in wanted)} to PR #{pr} "
            f"`{repo}` — user must be a collaborator/have push access on the repo. Assign manually in the UI.",
        )
    msg = f"✅ Assigned {', '.join('@'+a for a in added)} to PR #{pr} `{repo}`."
    if missed:
        msg += (
            f" ⚠️ Skipped {', '.join('@'+m for m in missed)} "
            "(no access on the repo — add them as a collaborator first)."
        )
    return ToolResult.success(msg)


async def approve_pr(
    pr: int, repo: str | None = None, body: str = "", confirmed: bool = False
) -> ToolResult:
    repo = _repo(repo)
    if not confirmed:
        question = f"Confirm **approve review** for PR #{pr} `{repo}`?"
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
            detail = (r.json() or {}).get("message") or "could not approve"
            return ToolResult.failure(
                "VALIDATION", f"GitHub refused to approve PR #{pr} `{repo}`: {detail}"
            )
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(
        f"✅ Approved review for PR #{pr} `{repo}`: <{d.get('html_url') or ''}|view review>"
    )


async def create_pr(
    title: str,
    head: str,
    base: str,
    body: str = "",
    repo: str | None = None,
    draft: bool = False,
) -> ToolResult:
    repo = _repo(repo)
    if not title.strip():
        return ToolResult.failure("VALIDATION", "PR title must not be empty.")
    if not head.strip() or not base.strip():
        return ToolResult.failure("VALIDATION", "head/base branch must not be empty.")

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
                        f"ℹ️ PR already exists: <{p['html_url']}|#{p['number']} {p['title']}>"
                    )
            detail = msg or errors_text or "PR creation refused"
            return ToolResult.failure(
                "VALIDATION", f"GitHub refused to create PR `{repo}`: {detail}"
            )
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(
        f"✅ Created PR #{d['number']} `{repo}`: <{d['html_url']}|{d['title']}>"
    )


async def update_pr(
    pr: int,
    repo: str | None = None,
    base: str | None = None,
    title: str | None = None,
    body: str | None = None,
    draft: bool | None = None,
) -> ToolResult:
    repo = _repo(repo)
    patch: dict = {}
    if base is not None:
        patch["base"] = base
    if title is not None:
        patch["title"] = title
    if body is not None:
        patch["body"] = body
    if draft is not None:
        patch["draft"] = draft
    if not patch:
        return ToolResult.failure("VALIDATION", "No fields to update.")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.patch(
            f"{API}/repos/{repo}/pulls/{pr}",
            headers=_headers(),
            json=patch,
        )
        if r.status_code == 422:
            data = r.json() or {}
            msg = data.get("message") or "PR update refused"
            return ToolResult.failure("VALIDATION", f"GitHub refused: {msg}")
        r.raise_for_status()
        d = r.json()
    parts = []
    if base is not None:
        parts.append(f"base → `{d['base']['ref']}`")
    if title is not None:
        parts.append(f"title → `{d['title']}`")
    if draft is not None:
        parts.append(f"draft → `{d['draft']}`")
    summary = ", ".join(parts) or "updated successfully"
    return ToolResult.success(
        f"✅ PR #{pr} `{repo}` updated: {summary}. <{d['html_url']}|View PR>"
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
            "VALIDATION", f"merge method `{method}` is invalid (squash/merge/rebase)"
        )

    # Always re-check mergeability — even on resume, state may have changed.
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/pulls/{pr}", headers=_headers())
        r.raise_for_status()
        p = r.json()

    if p.get("merged"):
        return ToolResult.failure(
            "VALIDATION", f"PR #{pr} `{repo}` is already merged (sha {p.get('merge_commit_sha')})."
        )
    if p.get("state") != "open":
        return ToolResult.failure(
            "VALIDATION", f"PR #{pr} `{repo}` is in state `{p.get('state')}`, cannot merge."
        )
    if p.get("draft"):
        return ToolResult.failure(
            "VALIDATION", f"PR #{pr} `{repo}` is a draft."
        )

    state = p.get("mergeable_state") or "unknown"
    if state not in _MERGE_OK_STATES:
        reasons = {
            "dirty": "has a conflict with the base branch",
            "blocked": "is blocked (missing required approvals or required checks failing)",
            "behind": "branch is behind base, needs a branch update",
            "draft": "PR is a draft",
            "unknown": "GitHub hasn't finished computing mergeable state, retry in a bit",
            "has_hooks": "a hook is blocking (needs admin merge)",
        }
        why = reasons.get(state, f"mergeable_state = `{state}`")
        return ToolResult.failure(
            "VALIDATION", f"⛔ PR #{pr} `{repo}` cannot be merged yet: {why}."
        )

    if not confirmed:
        head = p.get("head", {}).get("ref", "?")
        base = p.get("base", {}).get("ref", "?")
        question = (
            f"Confirm **merge** for PR #{pr} `{repo}` "
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
            detail = (r.json() or {}).get("message") or "merge refused"
            return ToolResult.failure(
                "VALIDATION", f"GitHub refused to merge PR #{pr} `{repo}`: {detail}"
            )
        r.raise_for_status()
        d = r.json()
    return ToolResult.success(
        f"✅ Merged PR #{pr} `{repo}` ({method}). SHA `{d.get('sha', '?')[:7]}`."
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
        return f"_No `{state}` PRs in `{repo}`._"
    lines = [f"*`{state}` PRs in `{repo}`* ({len(items)}):"]
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
        return f"_No `{state}` issues in `{repo}`._"
    lines = [f"*`{state}` issues in `{repo}`* ({len(items)}):"]
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
        return "_GitHub inbox is clean 🎉_"
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
        return f"_No results for `{query}`._"
    lines = [f"*{kind}* — `{query}` ({total} total, showing {min(len(items), 20)}):"]
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


# Hard ceiling on the returned diff, independent of the caller's `max_chars`.
# A result the SDK considers too large is spilled to a `tool-results/toolu_*.json`
# file and replaced by a pointer. That file is a SINGLE JSON line, so reading it
# back with Read's line-based offset/limit cannot shrink it under the 25k-token
# read limit — 2026-08-11 saw `max_chars=80000` on one PR produce four identical
# failed Reads and six `.{300}` Greps to scrape the spilled file 300 chars at a
# time. ~3 chars/token on diff text puts 40k chars near 13k tokens, well clear.
# A diff bigger than this is reviewed per-file, not by asking for more at once.
_MAX_DIFF_CHARS = 40000


async def get_pr_diff(pr: int, repo: str | None = None, max_chars: int = 20000) -> str:
    repo = _repo(repo)
    asked = max(1, int(max_chars))
    limit = min(asked, _MAX_DIFF_CHARS)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{API}/repos/{repo}/pulls/{pr}",
            headers=_headers(accept="application/vnd.github.v3.diff"),
        )
        r.raise_for_status()
        diff = r.text
    total = len(diff)
    if total > limit:
        capped = f" (max_chars {asked} capped at {_MAX_DIFF_CHARS})" if asked > limit else ""
        diff = (
            diff[:limit]
            + f"\n... [truncated at {limit} of {total} chars{capped}. Raising max_chars "
            "will not return more — read the rest from a `git_prepare_pr_review_workspace` "
            "checkout, or `git diff` the paths you still need.]"
        )
    return f"*PR #{pr} `{repo}` diff*:\n```\n{diff}\n```"


# ---------- dispatch ----------

ACTION_HANDLERS = {
    "github.create_issue":     lambda p: create_issue(p["title"], p["body"], p.get("repo")),
    "github.create_pr":        lambda p: create_pr(p["title"], p["head"], p["base"],
                                                   p.get("body", ""), p.get("repo"),
                                                   bool(p.get("draft", False))),
    "github.comment_pr":       lambda p: comment_pr(p["pr"], p["body"], p.get("repo")),
    "github.add_assignees":    lambda p: add_assignees(p["pr"], p["assignees"], p.get("repo")),
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
    "github.update_pr":        lambda p: update_pr(p["pr"], p.get("repo"),
                                                   p.get("base"), p.get("title"),
                                                   p.get("body"),
                                                   p["draft"] if "draft" in p else None),
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
