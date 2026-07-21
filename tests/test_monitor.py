"""Hermetic tests for the hourly health monitor — no live Loki/Slack/HTTP."""

from __future__ import annotations

import asyncio

import pytest

from agentic import monitor
from agentic.config import settings


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def chat_postMessage(self, **kwargs) -> dict:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1.0"}


def test_format_notable_when_over_threshold() -> None:
    svc = [{"name": "order-service", "count": 50}, {"name": "user-service", "count": 3}]
    text, notable = monitor._format(svc, [], env="prod", window="1h", threshold=20)
    assert notable is True
    assert "🔴 `order-service`" in text
    assert "50 error logs" in text
    # below-threshold service shown only as low-priority context, not as an alert
    assert "user-service` 3" in text


def test_format_notable_when_endpoint_down() -> None:
    health = [{"name": "api", "url": "https://x", "ok": False, "status": 503, "error": None}]
    text, notable = monitor._format([], health, env="prod", window="1h", threshold=20)
    assert notable is True
    assert "Endpoint DOWN" in text
    assert "HTTP 503" in text


def test_format_clean_is_not_notable() -> None:
    svc = [{"name": "order-service", "count": 2}, {"name": "user-service", "count": 0}]
    health = [{"name": "api", "url": "https://x", "ok": True, "status": 200, "error": None}]
    text, notable = monitor._format(svc, health, env="prod", window="1h", threshold=20)
    assert notable is False
    assert "✅" in text
    assert "1/1 endpoint OK" in text


def test_format_flags_failed_queries() -> None:
    svc = [{"name": "order-service", "count": None}]
    text, notable = monitor._format(svc, [], env="prod", window="1h", threshold=20)
    assert notable is False
    assert "Couldn't query Loki" in text
    assert "order-service" in text


@pytest.mark.asyncio
async def test_run_check_aggregates_counts(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "_target_services", lambda: [("order-service", "{job=\"x\"}")])
    monkeypatch.setattr(monitor, "_parse_health_urls", lambda: [])

    async def fake_count(selector, env, window):
        return 99

    async def fake_sample(selector, env, window):
        return ['{"level":"error","msg":"boom","status":500}'] * 3

    monkeypatch.setattr(monitor.grafana, "count_errors", fake_count)
    monkeypatch.setattr(monitor.grafana, "sample_errors", fake_sample)
    text, notable = await monitor.run_check()
    assert notable is True
    assert "99 error logs" in text
    # over-threshold service carries an inline error summary
    assert "3× boom" in text


def test_summarize_groups_across_formats() -> None:
    lines = [
        # Ruby/Rails 500s differing only in ids/timings — should collapse to one group
        "I, [2026-06-23T01:39:10 #68] INFO -- : [827ce664-b0f7-4c96-8fca-eef159fb63da] Completed 500 Internal Server Error in 358ms",
        "I, [2026-06-23T01:40:11 #70] INFO -- : [aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee] Completed 500 Internal Server Error in 12ms",
        # Go JSON crash
        '{"level":"error","msg":"db down","error":"timeout","status":503}',
    ]
    out = monitor._summarize_errors(lines)
    assert any(h.startswith("2× Completed 500 Internal Server Error") for h in out)
    assert any("db down · timeout" in h and h.startswith("1×") for h in out)
    # the noisy Ruby prefix + request id are stripped from the hint
    assert all("INFO -- :" not in h and "#68" not in h for h in out)


def test_summarize_fatal_without_inline_message() -> None:
    lines = ["F, [2026-06-23T01:36:30 #48] FATAL -- : [0ef06e17-c013-4ddb-afcb-fb0162b81ee0]"]
    out = monitor._summarize_errors(lines)
    assert out == ["1× FATAL (stacktrace on the next line — dig by request id)"]


def test_format_renders_samples_under_over_service() -> None:
    svc = [{"name": "da-api", "count": 12}]
    samples = {"da-api": ["11× Completed 500 Internal Server Error", "1× FATAL"]}
    text, notable = monitor._format(svc, [], env="prod", window="1h", threshold=5, samples=samples)
    assert "🔴 `da-api` — 12 error logs" in text
    assert "11× Completed 500" in text


@pytest.mark.asyncio
async def test_run_check_noop_when_nothing_to_check(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "_target_services", lambda: [])
    monkeypatch.setattr(monitor, "_parse_health_urls", lambda: [])
    text, notable = await monitor.run_check()
    assert text is None
    assert notable is False


@pytest.mark.asyncio
async def test_loop_posts_notable_then_respects_cancel(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(settings, "monitor_channel", "C123", raising=False)
    monkeypatch.setattr(settings, "monitor_always_post", False, raising=False)

    async def fake_check():
        return "alert text", True

    monkeypatch.setattr(monitor, "run_check", fake_check)

    async def fake_sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor.monitor_loop(client)
    assert client.posts == [{"channel": "C123", "text": "alert text"}]


@pytest.mark.asyncio
async def test_loop_skips_clean_when_not_always_post(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(settings, "monitor_channel", "C123", raising=False)
    monkeypatch.setattr(settings, "monitor_always_post", False, raising=False)

    async def fake_check():
        return "all green", False

    monkeypatch.setattr(monitor, "run_check", fake_check)

    async def fake_sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor.monitor_loop(client)
    assert client.posts == []


def test_start_monitor_disabled_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "monitor_enabled", False, raising=False)
    assert monitor.start_monitor(_FakeClient()) is None


def test_start_monitor_enabled_without_channel_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "monitor_enabled", True, raising=False)
    monkeypatch.setattr(settings, "monitor_channel", "", raising=False)
    assert monitor.start_monitor(_FakeClient()) is None
