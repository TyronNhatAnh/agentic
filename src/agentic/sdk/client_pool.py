"""Per-thread pool of ClaudeSDKClient instances.

One ClaudeSDKClient per Slack thread_ts, lives across turns so prompt cache +
conversation state survive. Idle clients are reaped after TTL to free
subprocess slots; capacity-bounded so a runaway thread can't exhaust the host.

Phase 0: minimal skeleton — `get_or_create`, `release`, `shutdown_all`. No
streaming yet; Phase 1 wires this into dev_agent and brain_session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from ..config import settings

log = logging.getLogger(__name__)

OptionsFactory = Callable[[str], Awaitable[ClaudeAgentOptions]]
"""Returns the ClaudeAgentOptions for a given thread_ts. Async so callers can
load thread state (repo, worktree, session resume token) from the DB."""


class ThreadSessionManager:
    def __init__(
        self,
        options_factory: OptionsFactory,
        *,
        idle_ttl_s: int | None = None,
        max_sessions: int | None = None,
    ) -> None:
        self._factory = options_factory
        self._idle_ttl_s = idle_ttl_s or settings.sdk_session_idle_ttl_s
        self._max_sessions = max_sessions or settings.sdk_max_concurrent_sessions
        # OrderedDict so LRU eviction is O(1) when capacity hit.
        self._clients: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get_or_create(self, thread_ts: str) -> ClaudeSDKClient:
        async with self._lock:
            entry = self._clients.get(thread_ts)
            if entry is not None:
                entry.last_used = time.monotonic()
                self._clients.move_to_end(thread_ts)
                return entry.client
            await self._evict_if_needed_locked()
            options = await self._factory(thread_ts)
            client = ClaudeSDKClient(options=options)
            await client.connect()
            self._clients[thread_ts] = _Entry(client=client, last_used=time.monotonic())
            log.info("sdk session opened thread=%s (active=%d)",
                     thread_ts, len(self._clients))
            return client

    async def release(self, thread_ts: str) -> None:
        """Explicit close — typically only used when thread is archived."""
        async with self._lock:
            entry = self._clients.pop(thread_ts, None)
        if entry is not None:
            await self._safe_close(entry.client, thread_ts)

    async def sweep_idle(self) -> int:
        """Close clients idle longer than TTL. Returns number closed."""
        now = time.monotonic()
        to_close: list[tuple[str, _Entry]] = []
        async with self._lock:
            for tts, entry in list(self._clients.items()):
                if now - entry.last_used > self._idle_ttl_s:
                    self._clients.pop(tts)
                    to_close.append((tts, entry))
        for tts, entry in to_close:
            await self._safe_close(entry.client, tts)
        return len(to_close)

    async def shutdown_all(self) -> None:
        async with self._lock:
            entries = list(self._clients.items())
            self._clients.clear()
        for tts, entry in entries:
            await self._safe_close(entry.client, tts)

    async def _evict_if_needed_locked(self) -> None:
        while len(self._clients) >= self._max_sessions:
            tts, entry = self._clients.popitem(last=False)
            log.warning("sdk pool full — evicting LRU thread=%s", tts)
            # Released outside the lock to keep critical section short.
            asyncio.create_task(self._safe_close(entry.client, tts))
            return

    async def _safe_close(self, client: ClaudeSDKClient, thread_ts: str) -> None:
        try:
            await client.disconnect()
        except Exception:
            log.exception("sdk session close failed thread=%s", thread_ts)


class _Entry:
    __slots__ = ("client", "last_used")

    def __init__(self, *, client: ClaudeSDKClient, last_used: float) -> None:
        self.client = client
        self.last_used = last_used
