import asyncio
import logging
import re
import subprocess
import time

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import settings
from .dispatcher import handle_message, init_sdk_singletons
from .monitor import start_monitor
from .sdk import (
    PendingPermissions,
    SqliteSessionStore,
    ThreadSessionManager,
    make_brain_options_factory,
)
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

    # SDK singletons. The Slack perm_allow/perm_deny button handlers need the
    # PendingPermissions instance; the brain pool's ClaudeSDKClient instances are
    # created lazily per thread on first use. dev/review/po/ba are
    # AgentDefinitions inside the brain session — there is no separate dev pool.
    session_store = SqliteSessionStore()
    pending = PendingPermissions()
    brain_factory = make_brain_options_factory(
        pending=pending,
        session_store=session_store,
        slack_client=app.client,
    )
    brain_pool = ThreadSessionManager(brain_factory)
    init_sdk_singletons(brain_pool=brain_pool, pending=pending)

    register(app, runner, pending=pending)
    sweeper = asyncio.create_task(
        _sdk_idle_sweeper(brain_pool), name="agentic-sdk-idle-sweep"
    )
    monitor = start_monitor(app.client)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    logging.getLogger(__name__).info(
        "⚡️ Bolt app started (Socket Mode), workers=%d",
        settings.worker_concurrency,
    )
    wake_watchdog = asyncio.create_task(
        _wake_reconnect_watchdog(handler), name="agentic-wake-watchdog"
    )
    try:
        await handler.start_async()
    finally:
        wake_watchdog.cancel()
        sweeper.cancel()
        if monitor:
            monitor.cancel()
        await brain_pool.shutdown_all()


async def _wake_reconnect_watchdog(
    handler: AsyncSocketModeHandler,
    *,
    tick_s: float = 300.0,
    jump_threshold_s: float = 30.0,
) -> None:
    """Force a fresh Socket Mode session after the host wakes from sleep.

    When the laptop lid closes, macOS suspends this process and severs the
    network. On wake, aiohttp's WebSocket resumes and ping/pong keeps flowing,
    so slack_bolt's lost-pong reconnect never fires — but Slack has already
    dropped the stale session and stops routing events to it (connection looks
    healthy, zero events arrive). We detect the suspend by watching for a clock
    jump far larger than our sleep interval, then rebuild the session via a new
    WSS endpoint so Slack routes events to us again.

    Detection reads BOTH clocks because their sleep behavior differs and is
    platform-dependent: macOS ``time.monotonic()`` (mach_absolute_time) freezes
    during suspend, so only the wall clock jumps; elsewhere the reverse can
    hold. Taking the larger elapsed catches the suspend under either behavior.
    A spurious fire (e.g. a rare NTP wall-clock correction) is harmless — it
    just re-establishes an equivalent session. tick_s bounds the worst-case
    recovery delay after wake (the sleep can't return until it elapses), so keep
    it modest; the per-tick cost is two clock reads.
    """
    log = logging.getLogger(__name__)
    while True:
        before_mono = time.monotonic()
        before_wall = time.time()
        try:
            await asyncio.sleep(tick_s)
        except asyncio.CancelledError:
            raise
        elapsed = max(time.monotonic() - before_mono, time.time() - before_wall)
        # A tick that took much longer than tick_s means the process was frozen
        # (sleep/suspend), not merely scheduled late.
        if elapsed < tick_s + jump_threshold_s:
            continue
        log.warning(
            "detected %.0fs process suspend (wake from sleep); forcing Socket "
            "Mode reconnect to shed stale session",
            elapsed,
        )
        try:
            await handler.client.connect_to_new_endpoint(force=True)
            log.info("Socket Mode reconnected to fresh endpoint after wake")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("wake reconnect failed; will retry on next suspend")


async def _sdk_idle_sweeper(*pools: ThreadSessionManager) -> None:
    """Reap idle SDK sessions every minute so subprocess slots free up."""
    while True:
        try:
            await asyncio.sleep(60)
            for pool in pools:
                closed = await pool.sweep_idle()
                if closed:
                    logging.getLogger(__name__).info(
                        "sdk idle sweep closed %d session(s)", closed
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("sdk idle sweep failed")


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
