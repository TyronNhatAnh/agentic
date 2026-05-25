import httpx

from ..config import settings

API = "https://api.github.com"


def _headers() -> dict[str, str]:
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN not configured")
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo(repo: str | None) -> str:
    repo = repo or settings.github_default_repo
    if not repo or "/" not in repo:
        raise ValueError("repo must be 'owner/name'")
    return repo


async def create_issue(title: str, body: str, repo: str | None = None) -> dict:
    repo = _repo(repo)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/issues",
            headers=_headers(),
            json={"title": title, "body": body},
        )
        r.raise_for_status()
        data = r.json()
        return {"number": data["number"], "url": data["html_url"]}


async def comment_pr(pr: int, body: str, repo: str | None = None) -> dict:
    repo = _repo(repo)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API}/repos/{repo}/issues/{pr}/comments",
            headers=_headers(),
            json={"body": body},
        )
        r.raise_for_status()
        return {"url": r.json()["html_url"]}


async def get_pr_diff(pr: int, repo: str | None = None) -> str:
    repo = _repo(repo)
    headers = _headers() | {"Accept": "application/vnd.github.v3.diff"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API}/repos/{repo}/pulls/{pr}", headers=headers)
        r.raise_for_status()
        return r.text


ACTION_HANDLERS = {
    "github.create_issue": lambda p: create_issue(p["title"], p["body"], p.get("repo")),
    "github.comment_pr": lambda p: comment_pr(p["pr"], p["body"], p.get("repo")),
}


async def execute_action(action_type: str, payload: dict) -> dict:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        raise ValueError(f"unknown action: {action_type}")
    return await handler(payload)
