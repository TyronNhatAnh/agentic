import asyncio
import logging

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


async def _main() -> None:
    _setup_logging()
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
