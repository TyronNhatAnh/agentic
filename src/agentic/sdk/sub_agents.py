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
from .permission import SESSION_DISALLOWED_TOOLS

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

# Safety boundary — never rewrite history or force-push. Defined once in
# permission.py (the gating module) and applied to both the dev sub-agent and the
# brain session; kept as DEV_DISALLOWED_TOOLS here for the dev AgentDefinition and
# existing references.
DEV_DISALLOWED_TOOLS = SESSION_DISALLOWED_TOOLS

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

# Archaeologist reads a legacy module and returns a design doc. Pure read — same
# rationale as review (a doc-producer must not be able to mutate): no Bash, no
# write. The one MCP tool it gets is `db_query` (read-only MariaDB introspection)
# because the legacy `db/schema.rb` is stale and config lives in DB rows — the live
# schema/config is recoverable only from the DB, never from source. The revamp
# pipeline also runs this prompt as a one-shot SDK query per module
# (revamp_pipeline.py); the AgentDefinition is the interactive entry point via the
# brain's Task tool.
ARCHAEOLOGIST_ALLOWED_TOOLS: list[str] = [
    "Read",
    "Glob",
    "Grep",
    "mcp__agentic__db_query",
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
        "archaeologist": AgentDefinition(
            description=(
                "Code archaeologist — đọc 1 module legacy (Ruby da-api) read-only "
                "và viết lại business logic thành tài liệu thiết kế (VERIFIED / "
                "HYPOTHESIS / MIGRATION PLAN). Dùng khi cần đào sâu hành vi hiện "
                "tại của một module để chuẩn bị viết mới. Không sửa code."
            ),
            prompt=load_prompt("archaeologist"),
            tools=ARCHAEOLOGIST_ALLOWED_TOOLS,
            mcpServers=["agentic"],
            model=settings.agent_model,
        ),
        "dev": AgentDefinition(
            description=(
                "Developer — đọc + sửa/viết code trong worktree (edit-only: KHÔNG "
                "có shell/git/gh). Trả về danh sách file đã đổi + commit message "
                "gợi ý; người gọi (brain) tự commit/push/mở PR. Dùng khi cần thay "
                "đổi code cho ticket đã có workspace."
            ),
            prompt=load_prompt("dev"),
            tools=DEV_ALLOWED_TOOLS,
            disallowedTools=DEV_DISALLOWED_TOOLS,
            model=settings.dev_model,
            permissionMode="acceptEdits",
        ),
    }
