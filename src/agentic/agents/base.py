import asyncio
import logging
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ClaudeRunError(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


async def run_claude(
    system_prompt: str,
    user_prompt: str,
    *,
    cwd: str | None = None,
    timeout: int | None = None,
    prompt_mode: str = "system",
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
) -> str:
    """Invoke `claude -p` as a subprocess and return stdout text.

    Relies on the user having `claude` installed and authenticated.
    """
    timeout = timeout or settings.claude_timeout
    run_cwd = cwd or settings.claude_runtime_dir
    Path(run_cwd).mkdir(parents=True, exist_ok=True)
    prompt_flag = "--append-system-prompt" if prompt_mode == "append" else "--system-prompt"
    args = [
        settings.claude_bin,
        "-p",
        user_prompt,
        prompt_flag,
        system_prompt,
        "--output-format",
        "text",
    ]
    if permission_mode:
        args.extend(["--permission-mode", permission_mode])
    # Scoped grants for this invocation only. The dev agent runs with cwd set to a
    # *service* worktree, so the bot's own .claude settings don't apply to it —
    # grants must travel on the command line. deny wins over allow, so force-push
    # and history-rewrite stay blocked even with `Bash(git push:*)` allowed.
    if allowed_tools:
        args.extend(["--allowedTools", *allowed_tools])
    if disallowed_tools:
        args.extend(["--disallowedTools", *disallowed_tools])
    # When using a non-default cwd (i.e. an actual repo), allow tool access to
    # the workspace root so claude can reach any service repo under it.
    # Falls back to the cwd itself if workspace_dir is not configured.
    if cwd and cwd != settings.claude_runtime_dir:
        allowed = settings.workspace_dir or cwd
        args.extend(["--add-dir", allowed])
    log.debug("running claude: %s chars sys / %s chars user", len(system_prompt), len(user_prompt))
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=run_cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeRunError(f"claude timed out after {timeout}s")
    if proc.returncode != 0:
        raise ClaudeRunError(stderr.decode("utf-8", errors="replace")[:4000])
    return stdout.decode("utf-8", errors="replace").strip()
