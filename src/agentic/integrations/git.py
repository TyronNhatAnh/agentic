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
from ..store import resolve_service
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


async def _resolve_base_branch(service: dict) -> str:
    """Format base branch using service template (or global default) + active sprint."""
    template = service.get("base_branch_template") or settings.base_branch_template
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
    repo_path = svc["repo_path"]
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
    "git.prepare_workspace": lambda p: prepare_workspace(
        p["service"], p["ticket"], bool(p.get("confirmed", False))
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
