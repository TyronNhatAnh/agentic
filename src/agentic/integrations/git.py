"""Git/worktree integration for local-repo dev flow.

Flow for `git.prepare_workspace`:
  1. Resolve service alias → repo metadata (path + base_branch_template).
  2. Query Jira active sprint, format base branch (e.g. `releases/DAPro-2.126`).
  3. `git fetch` the repo.
  4. If base branch missing locally AND not yet confirmed → return NEEDS_CONFIRMATION.
  5. Create worktree at WORKTREE_DIR/<service>/<ticket> on `feature/<ticket>` from base.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..config import settings
from ..store import resolve_service, resolve_service_by_github_repo
from . import jira as jira_int
from .result import ToolResult, classify_exception

_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


async def _run_git(*args: str, cwd: str | None = None,
                   env: dict[str, str] | None = None) -> tuple[int, str, str]:
    import os
    proc_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        env=proc_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode().strip(), err.decode().strip()


async def _authed_remote_url(repo_path: str) -> tuple[str, dict[str, str]]:
    """Return (remote_url, env_overrides) for authenticated remote operations.

    If GITHUB_TOKEN is set, returns an HTTPS+token URL plus env overrides that
    set GIT_CONFIG_GLOBAL=/dev/null to bypass ~/.gitconfig insteadOf rules
    (which would rewrite the HTTPS URL back to SSH). Falls back to "origin"
    (system auth) when no token is configured.
    """
    token = settings.github_token
    if not token:
        return "origin", {}
    _, remote_url, _ = await _run_git("remote", "get-url", "origin", cwd=repo_path)
    remote_url = remote_url.strip()
    if remote_url.startswith("git@github.com:"):
        path_part = remote_url[len("git@github.com:"):]
        https_url = f"https://x-access-token:{token}@github.com/{path_part}"
    elif remote_url.startswith("https://github.com/"):
        https_url = remote_url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/", 1)
    else:
        return "origin", {}
    # GIT_CONFIG_GLOBAL=/dev/null prevents git from loading ~/.gitconfig, which
    # may have insteadOf rules that rewrite HTTPS URLs back to SSH.
    return https_url, {"GIT_CONFIG_GLOBAL": "/dev/null"}


async def _branch_exists(repo_path: str, ref: str) -> bool:
    rc, _, _ = await _run_git("rev-parse", "--verify", "--quiet", ref, cwd=repo_path)
    return rc == 0


def worktree_path_for(svc: dict, ticket: str) -> Path:
    """Resolve the absolute worktree path for a service + ticket.

    Single source of truth — prepare/commit/push/ship all call this so the path
    can never diverge. The path MUST be absolute: `git worktree add` resolves a
    relative path against git's cwd (the repo), while later `Path.is_dir()` checks
    resolve against the bot process cwd — a relative path makes those two disagree
    and the worktree appears "missing" right after it was created.

    - WORKTREE_DIR set  → `<WORKTREE_DIR>/<service>/<ticket>`.
    - WORKTREE_DIR empty → `<repo_path>/.worktrees/<ticket>` (lives inside the
      service clone, same filesystem git prefers).
    """
    base = (settings.worktree_dir or "").strip()
    if base:
        return (Path(base).expanduser() / svc["name"] / ticket).resolve()
    repo_path = (svc.get("repo_path") or "").strip()
    return (Path(repo_path).resolve() / ".worktrees" / ticket)


async def find_worktree_for_branch(repo_path: str, branch: str) -> Path | None:
    """Locate an existing worktree checked out on `branch`, at whatever path.

    Resolving by branch (not by a recomputed path) means the worktree is still
    found after the path scheme changes, or if it was created by an older build.
    Returns the worktree path, or None if no worktree holds that branch.
    """
    rc, out, _ = await _run_git("worktree", "list", "--porcelain", cwd=repo_path)
    if rc != 0:
        return None
    target = f"refs/heads/{branch}"
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == f"branch {target}" and current:
            return Path(current)
    return None


async def resolve_existing_worktree(svc: dict, ticket: str) -> Path | None:
    """Find the worktree for a ticket: by branch first (any path), then the
    canonical path. Returns None if none exists on disk."""
    repo_path = (svc.get("repo_path") or "").strip()
    if repo_path:
        found = await find_worktree_for_branch(repo_path, f"feature/{ticket}")
        if found and found.is_dir():
            return found
    canonical = worktree_path_for(svc, ticket)
    if canonical.is_dir() and Path(canonical, ".git").exists():
        return canonical
    return None


async def check_repo(service: str | None = None, repo: str | None = None) -> ToolResult:
    """Read-only check for local service repository mapping and checkout status."""
    svc = None
    if repo:
        try:
            svc = resolve_service_by_github_repo(repo)
        except ValueError as e:
            return ToolResult.failure("VALIDATION", str(e))
    if not svc and service:
        svc = resolve_service(service)
    if not svc:
        target = repo or service or "repo/service"
        return ToolResult.failure(
            "NOT_FOUND",
            f"No local mapping for `{target}` in service_repos.",
        )

    # Empty repo_path → Path("") resolves to "." (the bot's own working dir, which
    # is itself a git repo) and would silently pass the .is_dir()/.git checks below,
    # reporting the agentic repo's branch as if it were the service. Reject explicitly.
    raw_path = (svc.get("repo_path") or "").strip()
    if not raw_path:
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` has no `repo_path` configured (no local clone). "
            "Set the path in services.json/service_repos first.",
        )
    repo_path = Path(raw_path)
    github_repo = svc.get("github_repo") or "(github_repo not set)"
    if not repo_path.is_dir():
        return ToolResult.failure(
            "CONFIG",
            f"Mapping `{svc['name']}` → `{repo_path}` exists, but the path does not.",
        )
    if not Path(repo_path, ".git").exists():
        return ToolResult.failure(
            "CONFIG",
            f"Path `{repo_path}` exists, but is not a git repo.",
        )

    rc, branch, _ = await _run_git("branch", "--show-current", cwd=str(repo_path))
    if rc != 0 or not branch:
        branch = "(detached or branch unreadable)"
    rc, status, _ = await _run_git("status", "--porcelain", cwd=str(repo_path))
    dirty = bool(status.strip()) if rc == 0 else None
    dirty_text = "dirty" if dirty else "clean"
    return ToolResult.success(
        f"Local repo for `{github_repo}`:\n"
        f"• Service: `{svc['name']}`\n"
        f"• Path: `{repo_path}`\n"
        f"• Branch: `{branch}`\n"
        f"• Status: `{dirty_text}`"
    )


async def latest_release_branch(service: str | None = None,
                                repo: str | None = None) -> ToolResult:
    """Fetch fresh remote state via GITHUB_TOKEN (HTTPS) and report the most
    recent ``releases/*`` branch + its HEAD commit.

    Root-cause path for "what's the latest release branch / commit" questions.
    A raw ``git fetch origin`` uses the SSH remote (``git@github.com:``), which
    cannot reach the user's ssh-agent from inside the sandboxed Bash — so it
    fails regardless of ``ssh-add``. Here we fetch over HTTPS+token like the
    other git tools, and pass an EXPLICIT refspec so ``refs/remotes/origin/
    releases/*`` is actually updated (a bare ``git fetch <url>`` only writes
    FETCH_HEAD, leaving remote-tracking refs stale — the bug that made the bot
    report an old commit).
    """
    svc = None
    if repo:
        try:
            svc = resolve_service_by_github_repo(repo)
        except ValueError as e:
            return ToolResult.failure("VALIDATION", str(e))
    if not svc and service:
        svc = resolve_service(service)
    if not svc:
        target = repo or service or "repo/service"
        return ToolResult.failure(
            "NOT_FOUND",
            f"No local mapping for `{target}` in service_repos.",
        )

    repo_path = (svc.get("repo_path") or "").strip()
    if not repo_path:
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` has no `repo_path` configured (no local clone). "
            "Set the path in services.json/service_repos first.",
        )
    if not Path(repo_path).is_dir() or not Path(repo_path, ".git").exists():
        return ToolResult.failure(
            "CONFIG",
            f"Repo path `{repo_path}` does not exist or is not a git repo.",
        )

    if not settings.github_token:
        return ToolResult.failure(
            "CONFIG",
            "GITHUB_TOKEN not set — can't fetch over HTTPS, and SSH doesn't work "
            "in the sandbox. Set GITHUB_TOKEN to read the latest remote state.",
        )

    fetch_url, authed_env = await _authed_remote_url(repo_path)
    if fetch_url == "origin":
        # Remote isn't a github URL we can rewrite to HTTPS+token.
        return ToolResult.failure(
            "CONFIG",
            "Remote origin is not a github URL that can be rewritten to HTTPS+token.",
        )
    # Explicit refspec → remote-tracking refs get updated (not just FETCH_HEAD).
    rc, _, err = await _run_git(
        "fetch", fetch_url,
        "+refs/heads/releases/*:refs/remotes/origin/releases/*",
        "--prune",
        cwd=repo_path, env=authed_env,
    )
    if rc != 0:
        return ToolResult.failure("GIT_FETCH", f"git fetch error: {err[:200]}")

    fmt = "%(refname:short)%09%(objectname)%09%(objectname:short)%09%(committerdate:iso8601)%09%(authorname)%09%(contents:subject)"
    rc, out, _ = await _run_git(
        "for-each-ref", "--sort=-committerdate", f"--format={fmt}",
        "refs/remotes/origin/releases/", cwd=repo_path,
    )
    lines = [l for l in out.splitlines() if l.strip()]
    if rc != 0 or not lines:
        return ToolResult.failure(
            "NOT_FOUND",
            f"No `releases/*` branch found on origin of `{svc['name']}`.",
        )
    parts = lines[0].split("\t")
    branch = parts[0].removeprefix("origin/")
    full_sha = parts[1] if len(parts) > 1 else "?"
    short_sha = parts[2] if len(parts) > 2 else full_sha[:9]
    date = parts[3] if len(parts) > 3 else "?"
    author = parts[4] if len(parts) > 4 else "?"
    subject = parts[5] if len(parts) > 5 else ""
    github_repo = (svc.get("github_repo") or "").strip()
    return ToolResult.success(
        f"Latest release branch of `{svc['name']}`"
        f"{f' ({github_repo})' if github_repo else ''}: `{branch}`\n"
        f"• Commit: `{full_sha}` (short `{short_sha}`)\n"
        f"• Message: {subject}\n"
        f"• Author: {author} — {date}\n"
        f"(fresh fetch over HTTPS+token, remote refs updated)"
    )


async def _resolve_base_branch(service: dict) -> str:
    """Format the base branch from the global template + active sprint.

    The base branch is locked org-wide to `settings.base_branch_template`
    (`releases/DAPro-2.{sprint}`); per-service `base_branch_template` overrides are
    intentionally ignored so every service branches off the same release line.
    """
    template = settings.base_branch_template
    if "{sprint}" not in template:
        return template
    board_id = service.get("jira_board_id") or settings.jira_board_id
    sprint = await jira_int.get_active_sprint(board_id or None)
    if sprint["number"] is None:
        raise RuntimeError(
            f"Could not parse sprint number from name `{sprint['name']}`"
        )
    return template.replace("{sprint}", str(sprint["number"]))


async def prepare_workspace(service: str, ticket: str,
                            confirmed: bool = False) -> ToolResult:
    if not _TICKET_RE.match(ticket):
        return ToolResult.failure(
            "VALIDATION", f"Ticket key `{ticket}` is invalid (expected form ABC-123)."
        )
    svc = resolve_service(service)
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND",
            f"Service `{service}` not found in mapping. "
            f"Check the service_repos table.",
        )
    repo_path = (svc.get("repo_path") or "").strip()
    if not repo_path:
        # Empty path would become Path(".") = the bot's own repo; never fetch/worktree there.
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` has no `repo_path` configured (no local clone). "
            "Set the path in services.json/service_repos first.",
        )
    if not Path(repo_path).is_dir() or not Path(repo_path, ".git").exists():
        return ToolResult.failure(
            "CONFIG",
            f"Repo path `{repo_path}` does not exist or is not a git repo. "
            f"Clone it first.",
        )

    # 1. Fetch
    fetch_url, authed_env = await _authed_remote_url(repo_path)
    rc, _, err = await _run_git("fetch", fetch_url, "--prune", cwd=repo_path, env=authed_env)
    if rc != 0:
        return ToolResult.failure("GIT_FETCH", f"git fetch error: {err[:200]}")

    # 2. Resolve base branch
    try:
        base = await _resolve_base_branch(svc)
    except Exception as e:
        return classify_exception(e, service="Jira")

    feature_branch = f"feature/{ticket}"

    # 2b. If a worktree for this ticket already exists (any path, incl. ones made
    # by an older build), reuse it — never recreate or ask about base.
    existing = await find_worktree_for_branch(repo_path, feature_branch)
    if existing and existing.is_dir():
        return ToolResult.success(
            {
                "service": svc["name"],
                "github_repo": (svc.get("github_repo") or "").strip(),
                "ticket": ticket,
                "base": base,
                "feature_branch": feature_branch,
                "worktree_path": str(existing),
                "message": (
                    f"📂 Worktree already exists at `{existing}` "
                    f"(branch `{feature_branch}`). Continue coding there."
                ),
            }
        )

    # 3. Check base exists locally or on origin
    local_has = await _branch_exists(repo_path, base)
    remote_has = await _branch_exists(repo_path, f"origin/{base}")

    if not remote_has and not local_has:
        # Nothing on remote either — fall back to latest release branch
        rc, out, _ = await _run_git(
            "for-each-ref", "--sort=-committerdate",
            "--format=%(refname:short)", "refs/remotes/origin/releases/",
            cwd=repo_path,
        )
        candidates = [b.removeprefix("origin/") for b in out.splitlines() if b.strip()]
        latest = candidates[0] if candidates else None
        if not latest:
            return ToolResult.failure(
                "NOT_FOUND",
                f"Neither base `{base}` nor any `releases/*` branch found on origin.",
            )
        if not confirmed:
            return _needs_confirmation(
                service=svc["name"], ticket=ticket, base=latest,
                question=(
                    f"Base `{base}` does not exist on origin. "
                    f"Latest release branch is `{latest}` — checkout from here? "
                    f"(reply: ok / no)"
                ),
            )
        base = latest
    elif not local_has and remote_has and not confirmed:
        return _needs_confirmation(
            service=svc["name"], ticket=ticket, base=base,
            question=(
                f"Base `{base}` not present locally (only on origin). "
                f"Pull it down and checkout a new worktree? (reply: ok / no)"
            ),
        )

    # 4. Create worktree at the canonical path (no existing one — checked in 2b).
    worktree_path = worktree_path_for(svc, ticket)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # If feature branch already exists, reuse it; else create from origin/base.
    if await _branch_exists(repo_path, feature_branch):
        rc, _, err = await _run_git(
            "worktree", "add", str(worktree_path), feature_branch, cwd=repo_path,
        )
    else:
        rc, _, err = await _run_git(
            "worktree", "add", "-b", feature_branch,
            str(worktree_path), f"origin/{base}",
            cwd=repo_path,
        )
    if rc != 0:
        return ToolResult.failure("GIT_WORKTREE", f"git worktree add error: {err[:300]}")

    return ToolResult.success(
        {
            "service": svc["name"],
            "ticket": ticket,
            "base": base,
            "feature_branch": feature_branch,
            "worktree_path": str(worktree_path),
            "message": (
                f"✅ Worktree ready:\n"
                f"• Service: `{svc['name']}`\n"
                f"• Base: `{base}`\n"
                f"• Branch: `{feature_branch}`\n"
                f"• Path: `{worktree_path}`\n"
                f"`cd {worktree_path}` to start coding."
            ),
        }
    )


async def commit_branch(service: str, ticket: str, message: str,
                        confirmed: bool = False) -> ToolResult:
    if not message.strip():
        return ToolResult.failure("VALIDATION", "Commit message must not be empty.")
    svc = resolve_service(service)
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND", f"Service `{service}` not found in mapping."
        )
    worktree_path = await resolve_existing_worktree(svc, ticket)
    if not worktree_path:
        return ToolResult.failure(
            "NOT_FOUND",
            f"No worktree for `feature/{ticket}`. Run `git.prepare_workspace` first.",
        )

    rc, status, err = await _run_git("status", "--porcelain", cwd=str(worktree_path))
    if rc != 0:
        return ToolResult.failure("GIT_STATUS", f"git status error: {err[:200]}")
    if not status.strip():
        return ToolResult.failure(
            "VALIDATION", f"Worktree `{worktree_path}` is clean, nothing to commit."
        )

    if not confirmed:
        lines = status.splitlines()
        preview = "\n".join(lines[:20])
        more = f"\n…(+{len(lines) - 20} files)" if len(lines) > 20 else ""
        question = (
            f"Commit in worktree `{worktree_path}`?\n"
            f"Message:\n> {message}\n"
            f"Files:\n```\n{preview}{more}\n```\n"
            f"(reply: ok / no)"
        )
        res = ToolResult.failure("NEEDS_CONFIRMATION", question)
        res.data = {
            "action_type": "git.commit",
            "payload": {"service": service, "ticket": ticket,
                        "message": message, "confirmed": True},
        }
        return res

    rc, _, err = await _run_git("add", "-A", cwd=str(worktree_path))
    if rc != 0:
        return ToolResult.failure("GIT_ADD", f"git add error: {err[:200]}")
    rc, _, err = await _run_git("commit", "-m", message, cwd=str(worktree_path))
    if rc != 0:
        return ToolResult.failure("GIT_COMMIT", f"git commit error: {err[:300]}")
    rc, sha, _ = await _run_git("rev-parse", "HEAD", cwd=str(worktree_path))
    sha_short = (sha or "?")[:7]
    return ToolResult.success(
        f"✅ Commit `{sha_short}` in `{worktree_path}`."
    )


async def push_branch(service: str, ticket: str) -> ToolResult:
    svc = resolve_service(service)
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND", f"Service `{service}` not found in mapping."
        )
    worktree_path = await resolve_existing_worktree(svc, ticket)
    if not worktree_path:
        return ToolResult.failure(
            "NOT_FOUND",
            f"No worktree for `feature/{ticket}`. Run `git.prepare_workspace` first.",
        )
    feature_branch = f"feature/{ticket}"
    push_url, authed_env = await _authed_remote_url(str(worktree_path))
    rc, _, err = await _run_git("push", "-u", push_url, feature_branch, cwd=str(worktree_path), env=authed_env)
    if rc != 0:
        return ToolResult.failure("GIT_PUSH", f"git push error: {err[:300]}")
    return ToolResult.success(f"✅ Pushed `{feature_branch}` to origin.")


async def prepare_pr_review_workspace(repo: str, pr: int) -> ToolResult:
    """Create or update a detached local worktree at the PR head for review."""
    try:
        svc = resolve_service_by_github_repo(repo)
    except ValueError as e:
        return ToolResult.failure("VALIDATION", str(e))
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND",
            f"No local repo mapping found for `{repo}` in service_repos.",
        )

    repo_path = (svc.get("repo_path") or "").strip()
    if not repo_path:
        # Empty path would become Path(".") = the bot's own repo; never fetch/worktree there.
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` has no `repo_path` configured (no local clone). "
            "Set the path in services.json/service_repos first.",
        )
    if not Path(repo_path).is_dir() or not Path(repo_path, ".git").exists():
        return ToolResult.failure(
            "CONFIG",
            f"Repo path `{repo_path}` does not exist or is not a git repo.",
        )

    fetch_ref = f"pull/{pr}/head"
    fetch_url, authed_env = await _authed_remote_url(repo_path)
    rc, _, err = await _run_git("fetch", fetch_url, fetch_ref, cwd=repo_path, env=authed_env)
    if rc != 0:
        return ToolResult.failure("GIT_FETCH", f"git fetch `{fetch_ref}` error: {err[:300]}")

    rc, sha, err = await _run_git("rev-parse", "--verify", "FETCH_HEAD", cwd=repo_path)
    if rc != 0 or not sha:
        return ToolResult.failure("GIT_FETCH", f"Could not resolve FETCH_HEAD: {err[:300]}")

    base = (settings.worktree_dir or "").strip()
    if base:
        worktree_path = (Path(base).expanduser() / "_pr_reviews" / svc["name"] / f"pr-{pr}").resolve()
    else:
        worktree_path = Path(repo_path).resolve() / ".worktrees" / "_pr_reviews" / f"pr-{pr}"
    if worktree_path.exists():
        if not Path(worktree_path, ".git").exists():
            return ToolResult.failure(
                "CONFIG",
                f"Review path `{worktree_path}` already exists but is not a git worktree.",
            )
        rc, status, err = await _run_git("status", "--porcelain", cwd=str(worktree_path))
        if rc != 0:
            return ToolResult.failure("GIT_STATUS", f"git status error: {err[:300]}")
        if status.strip():
            return ToolResult.failure(
                "DIRTY_WORKTREE",
                f"Review worktree `{worktree_path}` has local changes, not auto-checking out.",
            )
        rc, _, err = await _run_git("checkout", "--detach", sha, cwd=str(worktree_path))
        if rc != 0:
            return ToolResult.failure("GIT_CHECKOUT", f"git checkout PR head error: {err[:300]}")
    else:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = await _run_git(
            "worktree", "add", "--detach", str(worktree_path), sha, cwd=repo_path
        )
        if rc != 0:
            return ToolResult.failure("GIT_WORKTREE", f"git worktree add error: {err[:300]}")

    return ToolResult.success(
        {
            "service": svc["name"],
            "repo_path": str(worktree_path),
            "sha": sha,
            "message": (
                f"Local PR workspace ready: `{worktree_path}` "
                f"(service `{svc['name']}`, sha `{sha[:12]}`)."
            ),
        }
    )


def _needs_confirmation(*, service: str, ticket: str, base: str,
                        question: str) -> ToolResult:
    """Special ToolResult that the dispatcher persists as pending_confirmation."""
    res = ToolResult.failure("NEEDS_CONFIRMATION", question)
    res.data = {
        "action_type": "git.prepare_workspace",
        "payload": {"service": service, "ticket": ticket,
                    "base_hint": base, "confirmed": True},
    }
    return res


# ---------- dispatch ----------

ACTION_HANDLERS = {
    "git.check_repo": lambda p: check_repo(p.get("service"), p.get("repo")),
    "git.prepare_workspace": lambda p: prepare_workspace(
        p["service"], p["ticket"], bool(p.get("confirmed", False))
    ),
    "git.prepare_pr_review_workspace": lambda p: prepare_pr_review_workspace(
        p["repo"], int(p["pr"])
    ),
    "git.commit": lambda p: commit_branch(
        p["service"], p["ticket"], p["message"],
        bool(p.get("confirmed", False)),
    ),
    "git.push": lambda p: push_branch(p["service"], p["ticket"]),
    "git.latest_release": lambda p: latest_release_branch(
        p.get("service"), p.get("repo")
    ),
}


# Actions that fetch / create worktrees / commit / push against a repo. They are
# serialized per repo_path so two threads (e.g. the same ticket mentioned in two
# channels) can't race `git fetch` + `git worktree add` and leave a half-created
# worktree. check_repo is read-only and runs unlocked.
_MUTATING_ACTIONS = {
    "git.prepare_workspace",
    "git.prepare_pr_review_workspace",
    "git.commit",
    "git.push",
}

# Per-repo locks, created lazily. Bounded by the number of registered services.
_repo_locks: dict[str, asyncio.Lock] = {}
_repo_locks_guard = asyncio.Lock()


async def _repo_lock(repo_path: str) -> asyncio.Lock:
    async with _repo_locks_guard:
        lock = _repo_locks.get(repo_path)
        if lock is None:
            lock = asyncio.Lock()
            _repo_locks[repo_path] = lock
        return lock


def _repo_path_for_action(action_type: str, payload: dict) -> str | None:
    """Resolve the main clone path so concurrent ops on it serialize. Returns
    None when the service can't be resolved — the handler then reports the real
    NOT_FOUND/CONFIG error instead of us masking it with a lock miss."""
    try:
        if action_type == "git.prepare_pr_review_workspace":
            svc = resolve_service_by_github_repo(payload.get("repo", "") or "")
        else:
            svc = resolve_service(payload.get("service", "") or "")
    except ValueError:
        return None
    return ((svc or {}).get("repo_path") or "").strip() or None


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action_type}`")
    try:
        if action_type in _MUTATING_ACTIONS:
            repo_path = _repo_path_for_action(action_type, payload)
            if repo_path:
                lock = await _repo_lock(repo_path)
                async with lock:
                    return await handler(payload)
        return await handler(payload)
    except Exception as e:
        return classify_exception(e, service="git")
