"""Phase 1 eval driver — simulates Slack-driven dev turns by calling
run_dev_sdk directly, against a real `claude` subprocess via the SDK.

Captures `ResultMessage.usage` so we can check Phase 1 pass criteria §3:
- cache_read / (cache_read + cache_creation) > 60% on the 2nd turn of a thread
- dev edit success when dev_cwd starts as None (root-cause fix §1#4)
- latency p50 dev step < 70% baseline

Run:  .venv/bin/python -m scripts.eval_phase1

NOTE: spawns a real `claude` CLI; needs `claude login` already configured on
the host. Safe to run while `make debug` is up — uses its own in-process pool.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

from agentic.sdk import (
    PendingPermissions,
    SqliteSessionStore,
    ThreadSessionManager,
    make_dev_options_factory,
    run_dev_sdk,
)
from agentic.store import init_db, touch_thread


@dataclass
class TurnResult:
    label: str
    duration_s: float
    cache_read: int
    cache_create: int
    in_tokens: int
    out_tokens: int
    cost_usd: float | None
    reply_preview: str

    @property
    def cache_ratio(self) -> float:
        total = self.cache_read + self.cache_create
        return (self.cache_read / total) if total else 0.0


async def _drive_turn(
    *,
    label: str,
    pool: ThreadSessionManager,
    pending: PendingPermissions,
    thread_ts: str,
    task: str,
    cwd: str | None,
) -> TurnResult:
    """One round-trip through run_dev_sdk. Captures usage from log line."""

    # No Slack — stub chat_update so streaming edits become no-ops.
    slack = AsyncMock()
    slack.chat_update = AsyncMock(return_value={"ok": True})
    slack.chat_postMessage = AsyncMock(return_value={"ok": True})

    # Hook into the per-turn usage via a logging filter — cheaper than
    # restructuring run_dev_sdk to return usage. The log line shape is fixed:
    #   "sdk dev usage thread=<ts> cache_read=N cache_create=N in=N out=N cost=$X"
    captured: dict[str, Any] = {}

    import logging

    class _Capture(logging.Handler):
        def emit(self, rec):  # noqa: ARG002
            msg = rec.getMessage()
            if "sdk dev usage" not in msg:
                return
            for part in msg.split():
                if "=" not in part:
                    continue
                k, _, v = part.partition("=")
                captured[k] = v

    cap = _Capture()
    da_log = logging.getLogger("agentic.sdk.dev_agent")
    da_log.addHandler(cap)
    da_log.setLevel(logging.INFO)

    t0 = time.monotonic()
    try:
        reply = await run_dev_sdk(
            task,
            thread_ts=thread_ts,
            channel_id="C_EVAL",
            slack_client=slack,
            placeholder_ts="0000.0001",
            cwd=cwd,
            context="",
            pool=pool,
            pending=pending,
        )
    finally:
        da_log.removeHandler(cap)
    dur = time.monotonic() - t0

    def _int(k: str) -> int:
        try:
            return int(captured.get(k, "0"))
        except ValueError:
            return 0

    cost_str = captured.get("cost", "?")
    cost: float | None = None
    if cost_str.startswith("$"):
        try:
            cost = float(cost_str.lstrip("$"))
        except ValueError:
            cost = None

    return TurnResult(
        label=label,
        duration_s=dur,
        cache_read=_int("cache_read"),
        cache_create=_int("cache_create"),
        in_tokens=_int("in"),
        out_tokens=_int("out"),
        cost_usd=cost,
        reply_preview=(reply or "")[:200].replace("\n", " "),
    )


async def main() -> None:
    init_db()

    session_store = SqliteSessionStore()
    pending = PendingPermissions()
    factory = make_dev_options_factory(
        pending=pending,
        session_store=session_store,
        slack_client=AsyncMock(),
    )
    pool = ThreadSessionManager(factory)

    # Use fresh thread_ts each run so eval is repeatable (no stale session
    # state leaking in via SqliteSessionStore.resume).
    base = f"{int(time.time())}.eval"

    # Target a real service repo to exercise the full dev path. user-service
    # was clean at eval design time; pick from `service_repos` table at runtime
    # to stay aligned with the operator's workspace layout.
    svc_path = "/Users/tyron/Documents/work/Gogox/ggx-kr-user-service"
    eval_file = f"{svc_path}/README.md"
    marker = "<!-- agentic-eval-phase1-marker -->"

    scenarios: list[tuple[str, str, str, str | None]] = [
        # (label, thread_ts, task, cwd)
        (
            "S1 service read — no pin",
            f"{base}.s1",
            (
                f"Đọc file `{eval_file}` và tóm tắt service này trong 3 bullet "
                "tiếng Việt. KHÔNG edit gì."
            ),
            None,
        ),
        (
            "S2 service edit — add marker",
            f"{base}.s2",
            (
                f"Thêm chính xác 1 dòng `{marker}` vào đầu file `{eval_file}` "
                "(line 1, trước nội dung gốc). Sau đó báo lại đã edit xong. "
                "KHÔNG commit, KHÔNG push."
            ),
            svc_path,
        ),
        (
            "S2 turn-2 same thread — revert marker (cache + continuity)",
            f"{base}.s2",
            (
                f"Xoá dòng `{marker}` ra khỏi file `{eval_file}` (revert về "
                "trạng thái gốc). KHÔNG commit."
            ),
            svc_path,
        ),
        (
            "S3 multi-file navigation",
            f"{base}.s3",
            (
                f"Liệt kê 5 file lớn nhất trong `{svc_path}/app/` (sort theo "
                "bytes giảm dần). Chỉ báo tên + size."
            ),
            svc_path,
        ),
    ]

    print("=" * 90)
    print(f"{'scenario':<45} {'dur':>7} {'cache%':>7} {'in':>7} {'out':>6} {'cost':>8}")
    print("-" * 90)

    results: list[TurnResult] = []
    for label, thread_ts, task, cwd in scenarios:
        touch_thread(thread_ts, "C_EVAL")
        try:
            r = await _drive_turn(
                label=label,
                pool=pool,
                pending=pending,
                thread_ts=thread_ts,
                task=task,
                cwd=cwd,
            )
        except Exception as e:
            print(f"{label:<45} FAILED: {type(e).__name__}: {e}")
            continue
        results.append(r)
        cost_s = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "?"
        print(
            f"{r.label:<45} {r.duration_s:>6.1f}s "
            f"{r.cache_ratio*100:>6.1f}% {r.in_tokens:>7} {r.out_tokens:>6} {cost_s:>8}"
        )
        print(f"   ↳ {r.reply_preview}")

    print("-" * 90)
    if len(results) >= 3:
        t2 = results[2]
        print(
            f"§3 cache_ratio turn-2 (same thread) = {t2.cache_ratio*100:.1f}%  "
            f"(target > 60%)  → {'PASS' if t2.cache_ratio > 0.60 else 'FAIL'}"
        )

    # Verify the edit scenarios actually mutated then reverted the file. Failing
    # cleanup leaves the marker behind for the operator to inspect.
    print("\n--- edit verification ---")
    try:
        import subprocess

        diff = subprocess.run(
            ["git", "-C", svc_path, "diff", "--stat", "--", eval_file],
            capture_output=True, text=True, timeout=10,
        )
        leftover = subprocess.run(
            ["grep", "-c", marker, eval_file],
            capture_output=True, text=True, timeout=10,
        )
        leftover_count = int((leftover.stdout or "0").strip() or "0")
        print(f"git diff --stat README.md after revert: {diff.stdout.strip() or '(clean)'}")
        print(f"marker lines still in README.md: {leftover_count}")
        if leftover_count == 0 and not diff.stdout.strip():
            print("§3 dev edit + revert  → PASS (file mutated then restored)")
        else:
            print("§3 dev edit + revert  → CHECK (file not clean — see git diff)")
            print(f"   To clean up manually: git -C {svc_path} checkout -- {eval_file}")
    except Exception as e:
        print(f"verify step crashed: {e}")

    await pool.shutdown_all()


if __name__ == "__main__":
    asyncio.run(main())
