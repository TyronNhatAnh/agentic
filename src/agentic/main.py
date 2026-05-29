import asyncio
import logging
import re
import subprocess

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import settings
from .dispatcher import handle_message
from .slack_handlers import register
from .store import init_db
from .worker import JobRunner


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _check_claude_version() -> None:
    """Fail fast if `claude` binary is missing or below the SDK minimum.

    claude-agent-sdk requires Claude Code CLI >= 2.0.0 (see SDK
    MINIMUM_CLAUDE_CODE_VERSION). Surface the problem at startup with a clear
    message instead of letting the first request crash mid-flight.
    """
    try:
        out = subprocess.check_output(
            [settings.claude_bin, "--version"], stderr=subprocess.STDOUT, timeout=10
        ).decode("utf-8", errors="replace").strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise SystemExit(
            f"`{settings.claude_bin} --version` failed: {e}. "
            f"Install Claude Code CLI and run `claude login`."
        )
    found = _VERSION_RE.search(out)
    required = _VERSION_RE.search(settings.min_claude_version)
    if found and required:
        if tuple(map(int, found.groups())) < tuple(map(int, required.groups())):
            raise SystemExit(
                f"Claude Code CLI {out} is below required "
                f"{settings.min_claude_version}. Upgrade with `claude update`."
            )
    logging.getLogger(__name__).info("claude binary: %s", out)


async def _main() -> None:
    _setup_logging()
    _check_claude_version()
    init_db()
    if not settings.slack_bot_token or not settings.slack_app_token:
        raise SystemExit("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set")

    app = AsyncApp(token=settings.slack_bot_token)
    runner = JobRunner(handle_message, concurrency=settings.worker_concurrency)
    runner.start()
    register(app, runner)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    logging.getLogger(__name__).info(
        "⚡️ Bolt app started (Socket Mode), workers=%d", settings.worker_concurrency
    )
    await handler.start_async()


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
