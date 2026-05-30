"""Local validation driver for the SDK brain path.

Mock INPUT, real EXECUTION. Drives the real dispatcher entrypoint
(`handle_message`) with the real ThreadSessionManager / PendingPermissions /
brain options factory — i.e. real `claude` calls, real tokens/latency/cost,
real `runs` rows + per-tool hook rows. The only thing stubbed is Slack delivery:
a fake client captures every `chat.update` (so we can measure stream cadence /
flicker) and `chat.postMessage` (so we can auto-resolve permission Futures
without a human clicking a button).

What this CANNOT validate (needs a real Slack pass — see report):
  - the ✅/❌ button UX + the perm_allow/perm_deny action handler round-trip
  - perceived streaming flicker in the Slack UI

Usage:
  .venv/bin/python -m eval.driver po              # one scenario
  .venv/bin/python -m eval.driver po review multi  # several
  .venv/bin/python -m eval.driver all              # every scenario

Permission policy: confirm-gated tools (github_merge_pr/approve_pr) are
auto-DENIED here so the driver never performs an irreversible side effect; the
point is only to prove the callback FIRES. Pass `--allow-confirm` to auto-allow
(only do this against the scratch repo).
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agentic import dispatcher
from agentic.sdk import (
    PendingPermissions,
    SqliteSessionStore,
    ThreadSessionManager,
    make_brain_options_factory,
)
from agentic.store import init_db


# --------------------------------------------------------------------------- #
# Fake Slack client                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class Capture:
    """Per-thread record of what the brain pushed to "Slack"."""

    updates: list[tuple[float, int]] = field(default_factory=list)  # (monotonic, text_len)
    perm_posts: list[dict] = field(default_factory=list)            # {tool, req_id, allowed}

    def cadence_ms(self) -> list[int]:
        return [
            int((self.updates[i][0] - self.updates[i - 1][0]) * 1000)
            for i in range(1, len(self.updates))
        ]


class FakeSlackClient:
    """Implements only what brain_session / permission.py call: chat_update and
    chat_postMessage. Both async. chat_postMessage auto-resolves the pending
    permission Future so the brain doesn't block on a human."""

    def __init__(self, pending: PendingPermissions, *, allow_confirm: bool) -> None:
        self._pending = pending
        self._allow = allow_confirm
        self.cap = Capture()

    async def chat_update(self, *, channel: str, ts: str, text: str, **_: Any):
        self.cap.updates.append((time.monotonic(), len(text or "")))
        return {"ok": True, "ts": ts}

    async def chat_postMessage(self, *, channel: str, text: str = "",
                               blocks: list | None = None, **_: Any):
        req_id = _req_id_from_blocks(blocks)
        if req_id:
            tool = text  # the "❓ Cho phép `tool` chạy?" line
            self.cap.perm_posts.append(
                {"tool": tool, "req_id": req_id, "allowed": self._allow}
            )
            # Future was created by the callback just before this call; resolve
            # it synchronously — the callback is awaiting wait_for(fut) next.
            self._pending.resolve(req_id, self._allow)
        return {"ok": True, "ts": "perm-msg"}


def _req_id_from_blocks(blocks: list | None) -> str | None:
    for b in blocks or []:
        bid = b.get("block_id", "") if isinstance(b, dict) else ""
        if bid.startswith("perm:"):
            return bid[len("perm:"):]
    return None


# --------------------------------------------------------------------------- #
# Scenarios — the 5-ticket eval set (§9). Each is one or more user turns in a  #
# single thread. `expect` is a human-readable pass note, scored by eye + the   #
# metrics query afterwards.                                                    #
# --------------------------------------------------------------------------- #
@dataclass
class Scenario:
    id: str
    channel: str
    turns: list[str]
    expect: str
    side_effects: bool = False  # touches git/PR; needs scratch repo


SCENARIOS: dict[str, Scenario] = {
    # Ticket 4 — text-only PO/BA routing, no tools.
    "po": Scenario(
        id="po",
        channel="C_EVAL",
        turns=[
            "Giải thích ngắn gọn sự khác nhau giữa acceptance criteria và "
            "definition of done cho một user story.",
        ],
        expect="route po/ba (text-only), không gọi tool nào",
    ),
    # Probe — does the dev sub-agent actually get Bash? Forces delegation + a
    # single trivial Bash command. Cheap (1 short turn). If dev echoes the token,
    # bare "Bash" in dev.tools works; if it says no Bash, the grant is broken.
    "probe": Scenario(
        id="probe",
        channel="C_EVAL",
        turns=[
            "Giao cho sub-agent `dev` (KHÔNG tự làm hộ): dev chạy đúng MỘT lệnh "
            "Bash `echo DEV_BASH_OK_{STAMP}` rồi báo lại NGUYÊN VĂN stdout. Nếu dev "
            "không gọi được Bash thì nói rõ 'dev không chạy được Bash'. Không làm gì khác.",
        ],
        expect="dev chạy Bash echo → stdout DEV_BASH_OK_<stamp> xuất hiện = bare Bash works",
    ),
    # Ticket 5 — multi-turn same thread → session resume → cache_read cao.
    "multi": Scenario(
        id="multi",
        channel="C_EVAL",
        turns=[
            "Tóm tắt vai trò của brain session trong bot này là gì?",
            "Vậy sub-agent dev khác review ở điểm nào?",
            "Cho ví dụ khi nào brain nên gọi review thay vì tự trả lời.",
        ],
        expect="turn 2-3 cache_read ratio cao (session resume), không re-create session",
    ),
    # Ticket 2 — review fetch PR diff + cross-check (read-only). Needs a real
    # PR URL injected at runtime via --pr; otherwise skipped.
    "review": Scenario(
        id="review",
        channel="C_EVAL",
        turns=[
            "Review giúp PR này, tập trung correctness: {PR_URL}",
        ],
        expect="review sub-agent fetch github_get_pr_diff + nhận xét trong 1 turn",
    ),
    # Ticket 1 — dev edit+commit+push+PR end-to-end, không pin service.
    # Needs scratch repo seeded; {SVC} filled at runtime.
    "dev": Scenario(
        id="dev",
        channel="C_EVAL",
        turns=[
            "Giao cho sub-agent `dev` (nó tự chạy git/gh qua Bash): trong service "
            "{SVC}, thêm dòng '# eval smoke {STAMP}' vào cuối README, rồi dev tự "
            "commit + push feature branch + mở PR bằng Bash (git/gh), báo lại link "
            "PR. Ticket EVAL-3.",
        ],
        expect="dev resolve service từ registry → worktree → edit → commit → push → PR",
        side_effects=True,
    ),
    # Ticket 3 — merge PR → permission callback must fire.
    "merge": Scenario(
        id="merge",
        channel="C_EVAL",
        turns=["Merge giúp PR {PR_URL}"],
        expect="github_merge_pr → can_use_tool callback fire (chat_postMessage perm:)",
        side_effects=True,
    ),
}


# --------------------------------------------------------------------------- #
# Runner                                                                       #
# --------------------------------------------------------------------------- #
async def _setup(allow_confirm: bool) -> tuple[ThreadSessionManager, PendingPermissions, FakeSlackClient]:
    init_db()
    pending = PendingPermissions()
    fake = FakeSlackClient(pending, allow_confirm=allow_confirm)
    factory = make_brain_options_factory(
        pending=pending,
        session_store=SqliteSessionStore(),
        slack_client=fake,
    )
    pool = ThreadSessionManager(factory)
    dispatcher.init_sdk_singletons(brain_pool=pool, pending=pending)
    return pool, pending, fake


async def run_scenario(sc: Scenario, pool, pending, fake: FakeSlackClient,
                       subst: dict[str, str]) -> dict:
    thread_ts = f"eval-{sc.id}-{subst.get('STAMP','x')}"
    print(f"\n=== scenario '{sc.id}'  thread={thread_ts} ===")
    turn_stats = []
    for i, raw in enumerate(sc.turns, 1):
        text = raw
        for k, v in subst.items():
            text = text.replace("{" + k + "}", v)
        before = len(fake.cap.updates)
        t0 = time.monotonic()
        reply = await dispatcher.handle_message(
            text,
            thread_ts=thread_ts,
            channel=sc.channel,
            user_id="U_EVAL",
            thread_history=[],          # driver feeds turns explicitly via session
            slack_client=fake,
            placeholder_ts=f"ph-{sc.id}-{i}",
        )
        dt = time.monotonic() - t0
        n_upd = len(fake.cap.updates) - before
        print(f"  turn {i}: {dt:5.1f}s  updates={n_upd}")
        print(f"    > {text[:80]}")
        print(f"    < {reply[:200].replace(chr(10),' ')}")
        turn_stats.append({"turn": i, "secs": round(dt, 2), "updates": n_upd})
    return {
        "id": sc.id,
        "thread_ts": thread_ts,
        "turns": turn_stats,
        "perm_posts": list(fake.cap.perm_posts),
        "cadence_ms": fake.cap.cadence_ms(),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="+", help="po multi review dev merge | all")
    ap.add_argument("--allow-confirm", action="store_true",
                    help="auto-ALLOW confirm-gated tools (scratch repo only)")
    ap.add_argument("--pr", default="", help="PR URL for review/merge scenarios")
    ap.add_argument("--svc", default="", help="service name for dev scenario")
    ap.add_argument("--stamp", default=str(int(time.monotonic())),
                    help="unique suffix for thread_ts / commit text")
    ap.add_argument("--base", default="",
                    help="override settings.base_branch_template (e.g. 'main' for "
                         "the scratch repo, which has no releases/DAPro-2.N branch)")
    args = ap.parse_args()

    if args.base:
        # Driver-process only — the running bot keeps its sprint template.
        from agentic.config import settings as _s
        _s.base_branch_template = args.base
        print(f"[driver] base_branch_template overridden -> {args.base!r}")

    ids = list(SCENARIOS) if args.scenarios == ["all"] else args.scenarios
    subst = {"PR_URL": args.pr, "SVC": args.svc, "STAMP": args.stamp}

    pool, pending, fake = await _setup(args.allow_confirm)
    results = []
    try:
        for sid in ids:
            sc = SCENARIOS.get(sid)
            if not sc:
                print(f"!! unknown scenario {sid}")
                continue
            if sc.id in ("review", "merge") and not args.pr:
                print(f"!! skipping '{sid}' — needs --pr")
                continue
            if sc.id == "dev" and not args.svc:
                print(f"!! skipping 'dev' — needs --svc")
                continue
            # fresh capture per scenario
            fake.cap = Capture()
            results.append(await run_scenario(sc, pool, pending, fake, subst))
    finally:
        await pool.shutdown_all()

    print("\n========== SUMMARY ==========")
    for r in results:
        print(f"[{r['id']}] thread={r['thread_ts']}")
        for t in r["turns"]:
            print(f"   turn {t['turn']}: {t['secs']}s  {t['updates']} updates")
        if r["perm_posts"]:
            for p in r["perm_posts"]:
                print(f"   PERM FIRED: {p['tool']!r} allowed={p['allowed']}")
        if r["cadence_ms"]:
            c = r["cadence_ms"]
            print(f"   stream cadence ms: min={min(c)} max={max(c)} n={len(c)}")
    print("\nrun `make db-stats` + eval/metrics.sql for the §9 numbers.")


if __name__ == "__main__":
    asyncio.run(main())
