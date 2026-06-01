"""Hourly server-health monitor.

A single background asyncio task (spawned in [main.py] alongside the SDK idle
sweeper) that, every ``MONITOR_INTERVAL_S``:

1. counts ERROR-level Loki lines per registered service over ``MONITOR_WINDOW``
   (via :func:`grafana.count_errors`), and
2. GETs each configured ``MONITOR_HEALTH_URLS`` endpoint,

then posts a digest to the ``MONITOR_CHANNEL`` Slack channel.

By default it posts **only when something is notable** — a service crosses
``MONITOR_ERROR_THRESHOLD`` or a health endpoint is down — so a healthy hour
stays silent instead of spamming the channel. Set ``MONITOR_ALWAYS_POST=true``
to get a digest every cycle.

Deterministic by design: no brain/LLM call per cycle (cost). The digest is raw
signals; whoever reads it drills in via ``grafana_search_logs`` (or @-mentions
the bot). Timestamps follow the repo convention: UTC + VN(+7) + KST(+9).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import settings
from .integrations import grafana
from .store import list_services, resolve_service

log = logging.getLogger(__name__)

_HEALTH_TIMEOUT_S = 10


def _now_stamp() -> str:
    now = datetime.now(timezone.utc)
    vn = now + timedelta(hours=7)
    kr = now + timedelta(hours=9)
    return f"{now:%H:%M} UTC → {vn:%H:%M} VN / {kr:%H:%M} KST"


def _target_services() -> list[tuple[str, str]]:
    """(name, loki_selector) pairs to watch.

    ``MONITOR_SERVICES`` (CSV of names/aliases) narrows the list; empty = every
    registered service that has a non-empty ``loki_selector``.
    """
    names = [s.strip() for s in settings.monitor_services.split(",") if s.strip()]
    if names:
        out: list[tuple[str, str]] = []
        for n in names:
            svc = resolve_service(n)
            selector = (svc or {}).get("loki_selector", "").strip() if svc else ""
            if svc and selector:
                out.append((svc["name"], selector))
            else:
                log.warning("monitor: service `%s` unknown or has no loki_selector — skipped", n)
        return out
    return [
        (s["name"], (s.get("loki_selector") or "").strip())
        for s in list_services()
        if (s.get("loki_selector") or "").strip()
    ]


def _parse_health_urls() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in settings.monitor_health_urls.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            name, url = entry.split("=", 1)
            out.append((name.strip(), url.strip()))
        else:
            # derive a label from the host
            out.append((entry.split("//")[-1].split("/")[0], entry))
    return out


async def _check_service(name: str, selector: str) -> dict:
    count = await grafana.count_errors(
        selector, env=settings.monitor_env, window=settings.monitor_window
    )
    return {"name": name, "count": count}


async def _check_health(name: str, url: str) -> dict:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S, follow_redirects=True) as c:
            r = await c.get(url)
        latency = int((time.monotonic() - t0) * 1000)
        return {
            "name": name,
            "url": url,
            "ok": 200 <= r.status_code < 400,
            "status": r.status_code,
            "latency_ms": latency,
            "error": None,
        }
    except Exception as e:
        return {
            "name": name,
            "url": url,
            "ok": False,
            "status": None,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": type(e).__name__,
        }


def _format(
    svc_results: list[dict],
    health_results: list[dict],
    *,
    env: str,
    window: str,
    threshold: int,
) -> tuple[str, bool]:
    """Build the Slack digest and decide whether it is notable enough to post."""
    counted = sorted(
        (s for s in svc_results if s["count"] is not None),
        key=lambda s: s["count"],
        reverse=True,
    )
    over = [s for s in counted if s["count"] >= threshold]
    down = [h for h in health_results if not h["ok"]]
    failed_q = [s["name"] for s in svc_results if s["count"] is None]
    notable = bool(over or down)

    lines = [f"*🩺 Health check `{env}`* · {_now_stamp()} · cửa sổ {window}"]

    if down:
        lines.append("\n*Endpoint DOWN:*")
        for h in down:
            detail = h["error"] or f"HTTP {h['status']}"
            lines.append(f"🔴 `{h['name']}` — {detail} ({h['url']})")
    if over:
        lines.append(f"\n*Service vượt ngưỡng (≥ {threshold} log lỗi/{window}):*")
        for s in over:
            lines.append(f"🔴 `{s['name']}` — {s['count']} log lỗi")

    if notable:
        rest = [s for s in counted if 0 < s["count"] < threshold][:5]
        if rest:
            lines.append(
                "\n_Khác (dưới ngưỡng):_ "
                + ", ".join(f"`{s['name']}` {s['count']}" for s in rest)
            )
        ok_health = [h for h in health_results if h["ok"]]
        if ok_health:
            lines.append(f"_Endpoint OK: {len(ok_health)}/{len(health_results)}._")
    else:
        total = sum(s["count"] for s in counted)
        lines.append(f"✅ {len(svc_results)} service ổn (Σ {total} log lỗi, đều < {threshold}).")
        if health_results:
            ok = sum(1 for h in health_results if h["ok"])
            lines.append(f"Health: {ok}/{len(health_results)} endpoint OK.")

    if failed_q:
        lines.append(f"\n_⚠️ Không query được Loki cho: {', '.join(failed_q)}._")

    return "\n".join(lines), notable


async def run_check() -> tuple[str | None, bool]:
    """Run one monitor cycle. Returns (slack_text|None, notable)."""
    svcs = _target_services()
    healths = _parse_health_urls()
    if not svcs and not healths:
        log.warning(
            "monitor: no service has a loki_selector and no MONITOR_HEALTH_URLS set "
            "— nothing to check"
        )
        return None, False
    svc_results = (
        list(await asyncio.gather(*[_check_service(n, s) for n, s in svcs])) if svcs else []
    )
    health_results = (
        list(await asyncio.gather(*[_check_health(n, u) for n, u in healths])) if healths else []
    )
    return _format(
        svc_results,
        health_results,
        env=settings.monitor_env,
        window=settings.monitor_window,
        threshold=settings.monitor_error_threshold,
    )


async def monitor_loop(slack_client) -> None:
    log.info(
        "monitor: started, interval=%ds env=%s channel=%s always_post=%s",
        settings.monitor_interval_s,
        settings.monitor_env,
        settings.monitor_channel,
        settings.monitor_always_post,
    )
    while True:
        try:
            text, notable = await run_check()
            if text and (notable or settings.monitor_always_post):
                await slack_client.chat_postMessage(channel=settings.monitor_channel, text=text)
                log.info("monitor: posted digest (notable=%s)", notable)
            else:
                log.info("monitor: nothing to post (notable=%s)", notable)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("monitor: check cycle failed")
        await asyncio.sleep(settings.monitor_interval_s)


def start_monitor(slack_client) -> asyncio.Task | None:
    """Spawn the monitor task, or return None (with a warning) if not configured."""
    if not settings.monitor_enabled:
        return None
    if not settings.monitor_channel:
        log.warning("monitor: MONITOR_ENABLED set but MONITOR_CHANNEL empty — monitor not started")
        return None
    return asyncio.create_task(monitor_loop(slack_client), name="agentic-health-monitor")
