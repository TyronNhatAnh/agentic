import asyncio
import json
import logging
from contextvars import ContextVar
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Accumulates per-call usage dicts within a single handle_message() invocation.
# Dispatcher sets this up; background tasks (summarizer) run outside it.
_usage_tracker: ContextVar[list | None] = ContextVar("_usage_tracker", default=None)


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
    model: str | None = None,
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
        "json",
    ]
    if model:
        args.extend(["--model", model])
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
    # The prompt is passed via -p; claude must NOT read stdin. The bot runs as a
    # background daemon, so an inherited stdin (closed pipe / leftover bytes) makes
    # `claude -p` wait, then reply with a stub like "(responding now)" without ever
    # running its tools. Pin stdin to /dev/null so it proceeds immediately.
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=run_cwd,
        stdin=asyncio.subprocess.DEVNULL,
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
    raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        envelope = json.loads(raw)
        usage = envelope.get("usage") or {}
        in_tok = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        out_tok = usage.get("output_tokens", 0)
        cost = envelope.get("total_cost_usd")
        cost_str = f" cost=${cost:.4f}" if cost is not None else ""
        log.info("claude usage: in=%d out=%d%s", in_tok, out_tok, cost_str)
        tracker = _usage_tracker.get(None)
        if tracker is not None:
            tracker.append({
                "total_input_tokens": in_tok,
                "total_output_tokens": out_tok,
                "cost_usd": cost,
            })
        return envelope.get("result", raw)
    except (json.JSONDecodeError, AttributeError):
        return raw
