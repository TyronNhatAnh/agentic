"""Composite shipping flow: commit → push → open PR → transition Jira.

A single user confirm covers the whole sequence. Each sub-step runs the
underlying Python helper directly (not via execute_action) so the user is
not re-prompted on every leg.

Partial-failure policy: stop at first hard failure and report what
succeeded. Jira transition failure is treated as a warning — code/PR is
already in place by then.
"""
from __future__ import annotations

import re

import httpx

from ..store import resolve_service
from . import git as git_int
from . import github
from . import jira as jira_int
from .result import ToolResult, classify_exception

_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_DEFAULT_TARGET_STATUS = "In Review"


def _append_jira_key(body: str, ticket: str, title: str) -> str:
    body = (body or "").strip()
    if ticket in body or ticket in (title or ""):
        return body
    suffix = f"Jira: {ticket}"
    return f"{body}\n\n{suffix}".strip() if body else suffix


async def create_pr(
    service: str,
    ticket: str,
    pr_title: str,
    commit_message: str = "",
    pr_body: str = "",
    base: str | None = None,
    target_status: str = _DEFAULT_TARGET_STATUS,
    draft: bool = False,
) -> ToolResult:
    if not pr_title.strip():
        return ToolResult.failure("VALIDATION", "pr_title không được rỗng.")

    svc = resolve_service(service)
    if not svc:
        return ToolResult.failure(
            "NOT_FOUND", f"Không tìm thấy service `{service}` trong mapping."
        )
    worktree_path = await git_int.resolve_existing_worktree(svc, ticket)
    if not worktree_path:
        return ToolResult.failure(
            "NOT_FOUND",
            f"Chưa có worktree cho `feature/{ticket}`. Chạy `git.prepare_workspace` trước.",
        )
    github_repo = (svc.get("github_repo") or "").strip()
    if not github_repo or "/" not in github_repo:
        return ToolResult.failure(
            "CONFIG",
            f"Service `{svc['name']}` chưa cấu hình `github_repo` dạng `owner/name`.",
        )

    feature_branch = f"feature/{ticket}"

    # Resolve base if caller didn't pin one.
    if not base:
        try:
            base = await git_int._resolve_base_branch(svc)
        except Exception as e:
            return classify_exception(e, service="Jira")

    rc, status_out, err = await git_int._run_git(
        "status", "--porcelain", cwd=str(worktree_path)
    )
    if rc != 0:
        return ToolResult.failure("GIT_STATUS", f"git status lỗi: {err[:200]}")
    has_changes = bool(status_out.strip())

    final_body = _append_jira_key(pr_body, ticket, pr_title)


    lines: list[str] = []

    # 1. Commit (skip when nothing staged-or-modified, or no message given).
    if has_changes and commit_message.strip():
        rc, _, err = await git_int._run_git("add", "-A", cwd=str(worktree_path))
        if rc != 0:
            lines.append(f"1. ❌ git add lỗi: {err[:200]}")
            return ToolResult.failure("GIT_ADD", "\n".join(lines))
        rc, _, err = await git_int._run_git(
            "commit", "-m", commit_message, cwd=str(worktree_path)
        )
        if rc != 0:
            lines.append(f"1. ❌ git commit lỗi: {err[:300]}")
            return ToolResult.failure("GIT_COMMIT", "\n".join(lines))
        rc, sha, _ = await git_int._run_git(
            "rev-parse", "HEAD", cwd=str(worktree_path)
        )
        lines.append(f"1. ✅ Commit `{(sha or '?')[:7]}`")
    else:
        lines.append("1. ⏭️ Skip commit (worktree sạch)")

    # 2. Push (always run — branch might have local commits not pushed yet).
    push_url, authed_env = await git_int._authed_remote_url(str(worktree_path))
    rc, _, err = await git_int._run_git(
        "push", "-u", push_url, feature_branch, cwd=str(worktree_path), env=authed_env
    )
    if rc != 0:
        lines.append(f"2. ❌ git push lỗi: {err[:300]}")
        return ToolResult.failure("GIT_PUSH", "\n".join(lines))
    lines.append(f"2. ✅ Pushed `{feature_branch}` → origin")

    # 3. Open PR (or surface existing one).
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{github.API}/repos/{github_repo}/pulls",
                headers=github._headers(),
                json={
                    "title": pr_title,
                    "head": feature_branch,
                    "base": base,
                    "body": final_body,
                    "draft": draft,
                },
            )
            if r.status_code == 422:
                data = r.json() or {}
                msg = data.get("message") or ""
                errors_text = " ".join(
                    str(e.get("message", "")) for e in (data.get("errors") or [])
                )
                if "already exists" in (msg + errors_text).lower():
                    owner = github_repo.split("/", 1)[0]
                    async with httpx.AsyncClient(timeout=30) as c2:
                        r2 = await c2.get(
                            f"{github.API}/repos/{github_repo}/pulls",
                            headers=github._headers(),
                            params={"head": f"{owner}:{feature_branch}", "state": "open"},
                        )
                        r2.raise_for_status()
                        existing = r2.json()
                    if existing:
                        p = existing[0]
                        lines.append(
                            f"3. ℹ️ PR đã tồn tại: <{p['html_url']}|#{p['number']}>"
                        )
                    else:
                        lines.append(f"3. ❌ Tạo PR fail: {msg or errors_text}")
                        return ToolResult.failure("GITHUB_CREATE_PR", "\n".join(lines))
                else:
                    detail = msg or errors_text or "422"
                    lines.append(f"3. ❌ Tạo PR fail: {detail}")
                    return ToolResult.failure("GITHUB_CREATE_PR", "\n".join(lines))
            else:
                r.raise_for_status()
                d = r.json()
                lines.append(
                    f"3. ✅ PR <{d['html_url']}|#{d['number']}> — {d['title']}"
                )
    except Exception as e:
        lines.append(f"3. ❌ Tạo PR lỗi: {e}")
        return ToolResult.failure("GITHUB_CREATE_PR", "\n".join(lines))

    # 4. Jira transition — warning, not hard failure.
    try:
        trans = await jira_int.transition_issue(ticket, target_status)
        if trans.ok:
            lines.append(f"4. {trans.user_message}")
        else:
            lines.append(
                f"4. ⚠️ Jira transition fail ({trans.error_code}): {trans.user_message}"
            )
    except Exception as e:
        lines.append(f"4. ⚠️ Jira transition lỗi: {e}")

    return ToolResult.success("\n".join(lines))


ACTION_HANDLERS = {
    "ship.create_pr": lambda p: create_pr(
        p["service"], p["ticket"], p["pr_title"],
        p.get("commit_message", ""), p.get("pr_body", ""), p.get("base"),
        p.get("target_status", _DEFAULT_TARGET_STATUS),
        bool(p.get("draft", False)),
    ),
}


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action_type}`")
    try:
        return await handler(payload)
    except Exception as e:
        return classify_exception(e, service="ship")
