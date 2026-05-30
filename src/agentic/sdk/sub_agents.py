"""Sub-agent AgentDefinition entries for the brain session (Phase 3 — §3).

The brain delegates to dev/review/ba/po via the native ``Task`` tool. Each
``AgentDefinition`` carries its own prompt, tool subset, model, and
permission mode. They share the brain session's cwd + ``can_use_tool``
callback, so file edits in the dev sub-agent land in the same worktree the
brain pinned for the thread.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from ..agents.base import load_prompt
from ..config import settings

# Mirrors the legacy agents/dev.py allowlist plus the editor/search primitives
# the SDK exposes natively. Kept verbatim so dev still finishes edit → commit
# → push → open PR end-to-end inside a worktree.
# Dev is EDIT-ONLY. We verified (2026-05-30) that the SDK/CLI does not grant Bash
# to a sub-agent even when "Bash" is in `tools` AND in the session `allowed_tools`
# (both documented mechanisms) — the dev sub-agent never gets Bash in its palette
# (transcript shows it doesn't even attempt the call). So dev edits/inspects code
# in the worktree and the BRAIN orchestrates git/build (its own Bash + the MCP
# `git_*` / `ship_create_pr` tools). The old list also wrongly used permission-rule
# syntax ("Bash(git commit:*)") here, which `tools` (a bare-name allowlist) ignores.
DEV_ALLOWED_TOOLS: list[str] = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
]

# Safety boundary — never rewrite history or force-push.
DEV_DISALLOWED_TOOLS: list[str] = [
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git push --force-with-lease:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean -fd:*)",
    "Bash(git clean -f:*)",
    "Bash(git branch -D:*)",
]

# Review reads code + fetches PR data via MCP. It deliberately gets NO Bash:
# `tools` is a bare-name allowlist, so the old "Bash(git diff:*)" entries granted
# nothing anyway (rule syntax isn't a tool name); and bare "Bash" would be full
# shell — a review agent must not be able to commit/push/mutate (best practice:
# review reports, brain decides). Worktree cross-check is done by Read/Grep/Glob
# on the files; the PR diff comes from the MCP github tools. No write/push/comment.
REVIEW_ALLOWED_TOOLS: list[str] = [
    "Read",
    "Glob",
    "Grep",
    "mcp__agentic__github_get_pr",
    "mcp__agentic__github_get_pr_diff",
]


def build_subagents() -> dict[str, AgentDefinition]:
    """Return the AgentDefinition dict for ``ClaudeAgentOptions.agents``.

    Stable across thread sessions — prompt strings + tool lists are read at
    process start, so the brain's prefix cache stays warm.
    """
    return {
        "po": AgentDefinition(
            description=(
                "Product Owner — viết PRD, scope, milestones, definition of "
                "done. Dùng khi user xin product brief hoặc planning."
            ),
            prompt=load_prompt("po"),
            tools=[],
            model=settings.agent_model,
        ),
        "ba": AgentDefinition(
            description=(
                "Business Analyst — viết user story + acceptance criteria "
                "(Given/When/Then). Dùng khi user xin story cho feature."
            ),
            prompt=load_prompt("ba"),
            tools=[],
            model=settings.agent_model,
        ),
        "review": AgentDefinition(
            description=(
                "Code Reviewer — review PR diff theo template Markdown. Có "
                "thể fetch diff từ GitHub và cross-check file trong worktree. "
                "Dùng khi đã có PR hoặc patch cụ thể."
            ),
            prompt=load_prompt("review"),
            tools=REVIEW_ALLOWED_TOOLS,
            mcpServers=["agentic"],
            model=settings.agent_model,
        ),
        "dev": AgentDefinition(
            description=(
                "Developer — sửa/viết code trong worktree, chạy git/gh để "
                "commit, push feature branch, mở PR. Dùng khi thread đã có "
                "workspace cho ticket và user muốn fix/commit/push/PR."
            ),
            prompt=load_prompt("dev"),
            tools=DEV_ALLOWED_TOOLS,
            disallowedTools=DEV_DISALLOWED_TOOLS,
            model=settings.dev_model,
            permissionMode="acceptEdits",
        ),
    }
