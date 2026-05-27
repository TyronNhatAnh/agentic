from .base import load_prompt, run_claude

# Granted only when the dev agent edits inside a real worktree (apply_changes).
# Lets it finish the job end-to-end: edit → commit → push feature/* → open PR.
_DEV_ALLOWED_TOOLS = [
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git fetch:*)",
    "Bash(git rev-parse:*)",
    "Bash(git branch:*)",
    "Bash(git checkout:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr list:*)",
    "Bash(gh pr comment:*)",
]
# Safety boundary — never rewrite history or force-push, even inside a worktree.
_DEV_DISALLOWED_TOOLS = [
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git push --force-with-lease:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean -fd:*)",
    "Bash(git clean -f:*)",
    "Bash(git branch -D:*)",
]


async def run_dev(
    task: str,
    context: str = "",
    cwd: str | None = None,
    apply_changes: bool = False,
) -> str:
    system = load_prompt("dev")
    user = task if not context else f"{task}\n\n---\nContext:\n{context}"
    return await run_claude(
        system,
        user,
        cwd=cwd,
        prompt_mode="append",
        permission_mode="acceptEdits" if apply_changes else None,
        allowed_tools=_DEV_ALLOWED_TOOLS if apply_changes else None,
        disallowed_tools=_DEV_DISALLOWED_TOOLS if apply_changes else None,
    )
