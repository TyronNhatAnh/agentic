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


async def _run_git(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode().strip(), err.decode().strip()


async def _branch_exists(repo_path: str, ref: str) -> bool:
    rc, _, _ = await _run_git("rev-parse", "--verify", "--quiet", ref, cwd=repo_path)
    return rc == 0


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
            f"Chưa có mapping local cho `{target}` trong service_repos.",
        )

    # Empty repo_path → Path("") resolves to "." (the bot's own working dir, which
    # is itself a git repo) and would silently pass the .is_dir()/.git checks below,
    # reporting the agentic repo's branch as if it were the service. Reject explicitly.
    raw_path = (svc.get("repo_path") or "").strip()
    if not raw_path:
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` chưa cấu hình `repo_path` (chưa có clone local). "
            "Set path trong services.json/service_repos trước nhé.",
        )
    repo_path = Path(raw_path)
    github_repo = svc.get("github_repo") or "(chưa set github_repo)"
    if not repo_path.is_dir():
        return ToolResult.failure(
            "CONFIG",
            f"Có mapping `{svc['name']}` → `{repo_path}`, nhưng path chưa tồn tại.",
        )
    if not Path(repo_path, ".git").exists():
        return ToolResult.failure(
            "CONFIG",
            f"Có path `{repo_path}`, nhưng không phải git repo.",
        )

    rc, branch, _ = await _run_git("branch", "--show-current", cwd=str(repo_path))
    if rc != 0 or not branch:
        branch = "(detached hoặc không đọc được branch)"
    rc, status, _ = await _run_git("status", "--porcelain", cwd=str(repo_path))
    dirty = bool(status.strip()) if rc == 0 else None
    dirty_text = "dirty" if dirty else "clean"
    return ToolResult.success(
        f"Có repo local cho `{github_repo}` nha:\n"
        f"• Service: `{svc['name']}`\n"
        f"• Path: `{repo_path}`\n"
        f"• Branch: `{branch}`\n"
        f"• Status: `{dirty_text}`"
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
            f"Không parse được sprint number từ tên `{sprint['name']}`"
        )
    return template.replace("{sprint}", str(sprint["number"]))


async def prepare_workspace(service: str, ticket: str,
                            confirmed: bool = False) -> ToolResult:
    if not _TICKET_RE.match(ticket):
        return ToolResult.failure(
            "VALIDATION", f"Ticket key `{ticket}` không hợp lệ (cần dạng ABC-123)."
        )
    svc = resolve_service(service)
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND",
            f"Không tìm thấy service `{service}` trong mapping. "
            f"Kiểm tra bảng service_repos.",
        )
    repo_path = (svc.get("repo_path") or "").strip()
    if not repo_path:
        # Empty path would become Path(".") = the bot's own repo; never fetch/worktree there.
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` chưa cấu hình `repo_path` (chưa có clone local). "
            "Set path trong services.json/service_repos trước nhé.",
        )
    if not Path(repo_path).is_dir() or not Path(repo_path, ".git").exists():
        return ToolResult.failure(
            "CONFIG",
            f"Repo path `{repo_path}` chưa tồn tại hoặc chưa phải git repo. "
            f"Clone trước đi nhé.",
        )

    # 1. Fetch
    rc, _, err = await _run_git("fetch", "origin", "--prune", cwd=repo_path)
    if rc != 0:
        return ToolResult.failure("GIT_FETCH", f"git fetch lỗi: {err[:200]}")

    # 2. Resolve base branch
    try:
        base = await _resolve_base_branch(svc)
    except Exception as e:
        return classify_exception(e, service="Jira")

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
                f"Không thấy base `{base}` cũng không thấy branch `releases/*` nào trên origin.",
            )
        if not confirmed:
            return _needs_confirmation(
                service=svc["name"], ticket=ticket, base=latest,
                question=(
                    f"Base `{base}` không tồn tại trên origin. "
                    f"Branch release mới nhất là `{latest}` — checkout từ đây nhé? "
                    f"(reply: ok / không)"
                ),
            )
        base = latest
    elif not local_has and remote_has and not confirmed:
        return _needs_confirmation(
            service=svc["name"], ticket=ticket, base=base,
            question=(
                f"Base `{base}` chưa có local (chỉ có trên origin). "
                f"Pull về và checkout worktree mới? (reply: ok / không)"
            ),
        )

    # 4. Create worktree
    worktree_path = Path(settings.worktree_dir) / svc["name"] / ticket
    feature_branch = f"feature/{ticket}"

    if worktree_path.exists():
        return ToolResult.success(
            f"📂 Worktree đã có sẵn tại `{worktree_path}` (branch `{feature_branch}`). "
            f"Vào đó code tiếp nha."
        )

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
        return ToolResult.failure("GIT_WORKTREE", f"git worktree add lỗi: {err[:300]}")

    return ToolResult.success(
        f"✅ Worktree sẵn sàng:\n"
        f"• Service: `{svc['name']}`\n"
        f"• Base: `{base}`\n"
        f"• Branch: `{feature_branch}`\n"
        f"• Path: `{worktree_path}`\n"
        f"`cd {worktree_path}` để bắt đầu code."
    )


async def commit_branch(service: str, ticket: str, message: str,
                        confirmed: bool = False) -> ToolResult:
    if not _TICKET_RE.match(ticket):
        return ToolResult.failure(
            "VALIDATION", f"Ticket key `{ticket}` không hợp lệ (cần dạng ABC-123)."
        )
    if not message.strip():
        return ToolResult.failure("VALIDATION", "Commit message không được rỗng.")
    svc = resolve_service(service)
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND", f"Không tìm thấy service `{service}` trong mapping."
        )
    worktree_path = Path(settings.worktree_dir) / svc["name"] / ticket
    if not worktree_path.is_dir() or not Path(worktree_path, ".git").exists():
        return ToolResult.failure(
            "NOT_FOUND",
            f"Chưa có worktree `{worktree_path}`. Chạy `git.prepare_workspace` trước.",
        )

    rc, status, err = await _run_git("status", "--porcelain", cwd=str(worktree_path))
    if rc != 0:
        return ToolResult.failure("GIT_STATUS", f"git status lỗi: {err[:200]}")
    if not status.strip():
        return ToolResult.failure(
            "VALIDATION", f"Worktree `{worktree_path}` sạch, không có gì để commit."
        )

    if not confirmed:
        lines = status.splitlines()
        preview = "\n".join(lines[:20])
        more = f"\n…(+{len(lines) - 20} files)" if len(lines) > 20 else ""
        question = (
            f"Commit trong worktree `{worktree_path}`?\n"
            f"Message:\n> {message}\n"
            f"Files:\n```\n{preview}{more}\n```\n"
            f"(reply: ok / không)"
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
        return ToolResult.failure("GIT_ADD", f"git add lỗi: {err[:200]}")
    rc, _, err = await _run_git("commit", "-m", message, cwd=str(worktree_path))
    if rc != 0:
        return ToolResult.failure("GIT_COMMIT", f"git commit lỗi: {err[:300]}")
    rc, sha, _ = await _run_git("rev-parse", "HEAD", cwd=str(worktree_path))
    sha_short = (sha or "?")[:7]
    return ToolResult.success(
        f"✅ Commit `{sha_short}` trong `{worktree_path}`."
    )


async def push_branch(service: str, ticket: str,
                      confirmed: bool = False) -> ToolResult:
    if not _TICKET_RE.match(ticket):
        return ToolResult.failure(
            "VALIDATION", f"Ticket key `{ticket}` không hợp lệ (cần dạng ABC-123)."
        )
    svc = resolve_service(service)
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND", f"Không tìm thấy service `{service}` trong mapping."
        )
    worktree_path = Path(settings.worktree_dir) / svc["name"] / ticket
    if not worktree_path.is_dir() or not Path(worktree_path, ".git").exists():
        return ToolResult.failure(
            "NOT_FOUND",
            f"Chưa có worktree `{worktree_path}`. Chạy `git.prepare_workspace` trước.",
        )
    feature_branch = f"feature/{ticket}"

    if not confirmed:
        question = (
            f"Push branch `{feature_branch}` từ `{worktree_path}` lên origin? "
            f"(reply: ok / không)"
        )
        res = ToolResult.failure("NEEDS_CONFIRMATION", question)
        res.data = {
            "action_type": "git.push",
            "payload": {"service": service, "ticket": ticket, "confirmed": True},
        }
        return res

    rc, _, err = await _run_git(
        "push", "-u", "origin", feature_branch, cwd=str(worktree_path)
    )
    if rc != 0:
        return ToolResult.failure("GIT_PUSH", f"git push lỗi: {err[:300]}")
    return ToolResult.success(f"✅ Pushed `{feature_branch}` lên origin.")


async def prepare_pr_review_workspace(repo: str, pr: int) -> ToolResult:
    """Create or update a detached local worktree at the PR head for review."""
    try:
        svc = resolve_service_by_github_repo(repo)
    except ValueError as e:
        return ToolResult.failure("VALIDATION", str(e))
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND",
            f"Không tìm thấy local repo mapping cho `{repo}` trong service_repos.",
        )

    repo_path = (svc.get("repo_path") or "").strip()
    if not repo_path:
        # Empty path would become Path(".") = the bot's own repo; never fetch/worktree there.
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` chưa cấu hình `repo_path` (chưa có clone local). "
            "Set path trong services.json/service_repos trước nhé.",
        )
    if not Path(repo_path).is_dir() or not Path(repo_path, ".git").exists():
        return ToolResult.failure(
            "CONFIG",
            f"Repo path `{repo_path}` chưa tồn tại hoặc chưa phải git repo.",
        )

    fetch_ref = f"pull/{pr}/head"
    rc, _, err = await _run_git("fetch", "origin", fetch_ref, cwd=repo_path)
    if rc != 0:
        return ToolResult.failure("GIT_FETCH", f"git fetch `{fetch_ref}` lỗi: {err[:300]}")

    rc, sha, err = await _run_git("rev-parse", "--verify", "FETCH_HEAD", cwd=repo_path)
    if rc != 0 or not sha:
        return ToolResult.failure("GIT_FETCH", f"Không resolve được FETCH_HEAD: {err[:300]}")

    worktree_path = Path(settings.worktree_dir) / "_pr_reviews" / svc["name"] / f"pr-{pr}"
    if worktree_path.exists():
        if not Path(worktree_path, ".git").exists():
            return ToolResult.failure(
                "CONFIG",
                f"Path review `{worktree_path}` đã tồn tại nhưng không phải git worktree.",
            )
        rc, status, err = await _run_git("status", "--porcelain", cwd=str(worktree_path))
        if rc != 0:
            return ToolResult.failure("GIT_STATUS", f"git status lỗi: {err[:300]}")
        if status.strip():
            return ToolResult.failure(
                "DIRTY_WORKTREE",
                f"Review worktree `{worktree_path}` đang có thay đổi local, không tự checkout.",
            )
        rc, _, err = await _run_git("checkout", "--detach", sha, cwd=str(worktree_path))
        if rc != 0:
            return ToolResult.failure("GIT_CHECKOUT", f"git checkout PR head lỗi: {err[:300]}")
    else:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = await _run_git(
            "worktree", "add", "--detach", str(worktree_path), sha, cwd=repo_path
        )
        if rc != 0:
            return ToolResult.failure("GIT_WORKTREE", f"git worktree add lỗi: {err[:300]}")

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
    "git.push": lambda p: push_branch(
        p["service"], p["ticket"], bool(p.get("confirmed", False)),
    ),
}


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action_type}`")
    try:
        return await handler(payload)
    except Exception as e:
        return classify_exception(e, service="git")
