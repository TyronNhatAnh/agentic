"""Java (Tomcat) log reads over the Pi bastion — the web-* apps, api-layer and
node-message are files on EC2, not in Loki.

Wraps `jlog.sh` instead of re-implementing it: the wrapper owns the two-prod-node
fan-out (querying one node is a false-negative source) and the UTC/KST split.
Auth (Cloudflare Access + SSH key) belongs to the host user; expiry needs a human,
hence AUTH rather than a retry.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
from pathlib import Path

from ..config import settings
from .result import ToolResult

# Lives in the Claude Code skill dir, not this repo — the bot and the skill share
# one wrapper so a fix to either reaches both. Overridable for a different host.
_DEFAULT_SCRIPT = Path.home() / ".claude" / "skills" / "gogox-java-logs" / "jlog.sh"

APPS = {
    "web-admin", "web-api", "web-b2b", "web-b2c", "web-driver",
    "catalina", "access", "apl", "msg", "ls",
}
ENVS = {"stag", "prod"}

# jlog's own ceiling is `timeout 120 ssh` per node × 2 prod nodes, run in sequence,
# plus fmt.py — so a 240s cap would fire on the slow path instead of letting the
# wrapper report per-node. Sit above it.
_TIMEOUT_S = 300
# Cap what reaches the transcript: every returned line is re-read on each later
# turn of the thread. The wrapper's own `-m` already trims; this is the backstop.
_MAX_CHARS = 12000


def _script() -> Path:
    # os.environ first so a shell/service-manager export still wins over `.env`.
    return Path(os.environ.get("JLOG_SCRIPT") or settings.jlog_script or _DEFAULT_SCRIPT)


# `file` and `kst` are interpolated *unquoted* into the command jlog.sh sends over
# ssh (and `kst` also into a python -c source), so anything but a plain filename /
# timestamp is a shell injection on the bastion. Allowlist, not escaping.
_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_KST_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def _validate(
    env: str, app: str, grep: str, vgrep: str, file: str, kst: str
) -> ToolResult | None:
    if env not in ENVS:
        return ToolResult.failure("VALIDATION", f"env must be stag|prod, got `{env}`.")
    if app not in APPS:
        return ToolResult.failure(
            "VALIDATION",
            f"Unknown app `{app}`. Valid: {', '.join(sorted(APPS))}.",
        )
    # The filter crosses an EC2 shell; the wrapper rejects these itself, but a
    # clear message here saves a bastion round-trip.
    for name, pat in (("grep", grep), ("exclude", vgrep)):
        if "'" in pat:
            return ToolResult.failure(
                "VALIDATION",
                f"`{name}` can't contain a single quote (it breaks quoting on the "
                "bastion) — use `.` as a wildcard instead.",
            )
    if file and not _FILE_RE.match(file):
        return ToolResult.failure(
            "VALIDATION",
            f"`file` must be a bare log filename (letters, digits, `.`, `_`, `-`), "
            f"got `{file}`. Run app=`ls` to see the real names.",
        )
    if kst and not _KST_RE.match(kst):
        return ToolResult.failure(
            "VALIDATION", f"`kst` must be 'YYYY-MM-DD HH:MM', got `{kst}`."
        )
    return None


async def search(
    env: str = "prod",
    app: str = "web-api",
    grep: str = "",
    exclude: str = "",
    lines: int = 2000,
    tail: int = 80,
    count: bool = False,
    pretty: bool = True,
    fast: bool = False,
    kst: str = "",
    file: str = "",
    node: str = "",
) -> ToolResult:
    """Read one app's log through the bastion. Read-only on prod."""
    if err := _validate(env, app, grep, exclude, file, kst):
        return err
    script = _script()
    if not script.is_file():
        return ToolResult.failure(
            "CONFIG",
            f"jlog.sh not found at `{script}` — set JLOG_SCRIPT to the wrapper path.",
        )
    if not shutil.which("ssh"):
        return ToolResult.failure("CONFIG", "ssh not available on this host.")

    argv: list[str] = [str(script), env, app]
    if grep:
        argv += ["-g", grep]
    if exclude:
        argv += ["-v", exclude]
    if count:
        argv += ["-c"]
    else:
        argv += ["-m", str(max(1, int(tail)))]
    # `-p` only parses the JSON log shape; the access log and api-layer are plain
    # text and the wrapper would have nothing to expand.
    if pretty and app in {"web-admin", "web-api", "web-b2b", "web-b2c", "web-driver", "catalina"}:
        argv += ["-p"]
    if fast:
        argv += ["-F"]
    else:
        argv += ["-n", str(max(1, int(lines)))]
    if kst:
        argv += ["-k", kst]
    if file:
        argv += ["-f", file]
    if node in {"1", "2"}:
        argv += [f"-{node}"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "SSH_ASKPASS": "", "BATCH": "1"},
            # Own process group so a timeout can kill jlog.sh *and* the ssh it
            # spawned, without signalling the bot itself.
            start_new_session=True,
        )
        raw_out, raw_err = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_S
        )
    except TimeoutError:
        # wait_for only cancels the await; the tree keeps pulling log files off prod.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        return ToolResult.failure(
            "TIMEOUT",
            f"jlog timed out after {_TIMEOUT_S}s. Narrow it: pass `fast=true` with a "
            "bare token (greps on EC2, ~8x faster) or a smaller `lines`.",
            retryable=False,
        )
    except OSError as e:
        return ToolResult.failure("CONFIG", f"Could not run jlog.sh: {e}")

    out = raw_out.decode(errors="replace").strip()
    err = raw_err.decode(errors="replace").strip()
    rc = proc.returncode or 0

    # rc 3 = the wrapper's false-negative guard: a node errored, so an empty
    # result does NOT mean "no such log". Surface it as an error, not as "none".
    if rc == 3:
        return ToolResult.failure(
            "SERVER",
            "One prod node failed to answer, so an empty result proves nothing. "
            f"Retry, or pin the healthy node with `node`.\n{(err or out)[:500]}",
            retryable=True,
        )
    if rc != 0:
        blob = f"{err}\n{out}".lower()
        if "cloudflare" in blob or "access denied" in blob or "permission denied" in blob:
            return ToolResult.failure(
                "AUTH",
                "Bastion rejected auth — the Cloudflare Access session likely expired. "
                "It needs a human to re-authorize in a browser on the host; tell the "
                "user, don't retry.",
            )
        return ToolResult.failure(
            "VALIDATION",
            f"jlog exited {rc}: {(err or out)[:600]}",
        )
    if not out:
        return ToolResult.success(
            f"_No matching lines in `{env}/{app}`._ "
            "(Both prod nodes answered, so this is a real zero — widen the window "
            "with `kst`/`lines`, or check a rotated file via `file`.)"
        )
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS] + f"\n… truncated at {_MAX_CHARS} chars — narrow the grep."
    return ToolResult.success(out)


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    if action_type == "java_logs.search":
        return await search(**payload)
    return ToolResult.failure("VALIDATION", f"Unknown action `{action_type}`.")
