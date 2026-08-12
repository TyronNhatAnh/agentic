"""Regressions for the tool failures found in the 2026-08-10..12 `runs` audit.

Each test names the failure it locks down; the counts are how many times that
call actually died in those three days.
"""

from pathlib import Path

import pytest

from agentic.agents.base import DOCS_DIR, load_prompt
from agentic.integrations import java_logs as jl
from agentic.integrations.git import _EMBEDDED_CRED_RE
from agentic.integrations.grafana import _MAX_SPAN_NS, _clamp_span

_H = 3600 * 1_000_000_000


# --- 5x "Read: File does not exist" on the arch map -------------------------

@pytest.mark.parametrize("name", ["brain_sdk", "review"])
def test_prompt_doc_paths_are_absolute_and_real(name):
    """A relative `docs/...` in a prompt resolves against the brain's cwd (the
    thread's service repo), not this one."""
    text = load_prompt(name)
    assert "{DOCS}" not in text
    for doc in ("GOGOX_ARCHITECTURE.md", "arch/features.md", "arch/<service>.md"):
        # The bug was the bare relative form; `{DOCS}/...` expands to an absolute
        # path, so any surviving `docs/<doc>` is an instruction pointing nowhere.
        assert f"`docs/{doc}`" not in text, f"relative path to {doc} left in prompt"
    for doc in ("GOGOX_ARCHITECTURE.md", "arch/features.md"):
        if str(DOCS_DIR / doc) in text:
            assert (DOCS_DIR / doc).is_file()


def test_brain_prompt_carries_no_dead_doc_path():
    """Every absolute docs path the prompt hands the brain must resolve. This is
    the whole failure mode: a path that reads fine and points at nothing."""
    import re

    text = load_prompt("brain_sdk") + load_prompt("review")
    paths = set(re.findall(rf"{re.escape(str(DOCS_DIR))}/[A-Za-z0-9_/.-]+\.md", text))
    assert paths, "prompt should reference at least one doc"
    missing = [p for p in paths if not Path(p).is_file()]
    assert not missing, f"prompt points at nonexistent docs: {missing}"


def test_tool_descriptions_carry_no_relative_doc_path():
    """Tool descriptions ship in every request's schema and told the brain to Read
    `docs/arch/...` — same dead relative path as the prompts, different surface."""
    import re

    from agentic.sdk.mcp_tools import _ALL_TOOLS

    for t in _ALL_TOOLS:
        assert not re.search(r"(?<![\w/])docs/", t.description or ""), (
            f"{t.name} description points at a relative docs path"
        )


def test_review_agent_owns_every_mcp_tool_its_prompt_names():
    """review.md told it to call git_prepare_pr_review_workspace, which wasn't in
    its allowlist — so it could only ever see the (truncatable) diff text."""
    import re

    from agentic.sdk.sub_agents import REVIEW_ALLOWED_TOOLS

    text = load_prompt("review")
    named = set(re.findall(r"`(git_[a-z_]+|github_[a-z_]+|grafana_[a-z_]+)\(", text))
    missing = {t for t in named if f"mcp__agentic__{t}" not in REVIEW_ALLOWED_TOOLS}
    assert not missing, f"review prompt calls tools it cannot use: {missing}"


def test_jlog_script_override_is_reachable_from_dotenv(monkeypatch):
    """Nothing loads `.env` into os.environ, so a knob read only from os.environ
    would be dead for every user who configures it the documented way."""
    from agentic.config import settings

    monkeypatch.delenv("JLOG_SCRIPT", raising=False)
    monkeypatch.setattr(settings, "jlog_script", "/tmp/from-dotenv.sh")
    assert str(jl._script()) == "/tmp/from-dotenv.sh"
    monkeypatch.setenv("JLOG_SCRIPT", "/tmp/from-shell.sh")
    assert str(jl._script()) == "/tmp/from-shell.sh", "shell export must win"


# --- 2x Loki TIMEOUT on now-24h / now-7d ------------------------------------

def test_clamp_span_leaves_short_window_untouched():
    end = 1_000_000 * _H
    start = end - _H
    s, e, note = _clamp_span(str(start), str(end))
    assert (s, e, note) == (str(start), str(end), "")


def test_clamp_span_keeps_newest_slice_and_says_so():
    end = 1_000_000 * _H
    s, e, note = _clamp_span(str(end - 24 * _H), str(end))
    assert int(e) - int(s) == _MAX_SPAN_NS
    assert e == str(end), "must keep the newest slice, not the oldest"
    assert "24.0h" in note and "2h" in note


def test_clamp_span_boundary_is_not_clamped():
    end = 1_000_000 * _H
    _, _, note = _clamp_span(str(end - _MAX_SPAN_NS), str(end))
    assert note == ""


def test_clamp_span_tolerates_the_drift_between_two_now_reads():
    """`since` and `until` resolve via separate time.time_ns() calls, so an exact
    `now-2h`→`now` lands just over the cap and used to report a bogus 2h→2h
    narrowing (seen live)."""
    end = 1_000_000 * _H
    _, _, note = _clamp_span(str(end - _MAX_SPAN_NS - 3_000_000), str(end))
    assert note == ""


async def test_search_logs_reports_the_window_it_actually_queried(monkeypatch):
    """The clamp is invisible unless the result says so: the header used to echo
    `now-24h` while only the newest 2h was queried, and the zero-result advice
    told the brain to widen — which replays the same slice."""
    from agentic.integrations import grafana as gf

    monkeypatch.setattr(gf, "_env_conf", lambda env: ("https://g", "loki", "prod"))
    monkeypatch.setattr(gf, "_basic_auth", lambda: None)
    seen: dict = {}

    class _R:
        status_code = 200
        headers: dict = {}
        def raise_for_status(self): pass
        def json(self): return {"data": {"resultType": "streams", "result": []}}

    class _C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None, auth=None):
            seen.update(params)
            return _R()

    monkeypatch.setattr(gf.httpx, "AsyncClient", lambda **k: _C())
    r = await gf.search_logs(query='{app="x"}', env="prod", since="now-24h")
    assert int(seen["end"]) - int(seen["start"]) == _MAX_SPAN_NS
    assert "now-24h" not in r.data, "reported a window it never queried"
    assert "back another 2h" in r.data


def test_clamp_span_still_clamps_past_the_slack():
    end = 1_000_000 * _H
    s, _, note = _clamp_span(str(end - _MAX_SPAN_NS - 10 * 60 * 1_000_000_000), str(end))
    assert note and int(end) - int(s) == _MAX_SPAN_NS


# --- 1x "Remote origin is not a github URL that can be rewritten" -----------

@pytest.mark.parametrize(
    "url,path",
    [
        ("https://x-access-token:ghp_x@github.com/gogovan-korea/node-message.git",
         "gogovan-korea/node-message.git"),
        ("https://user:pass@github.com/a/b.git", "a/b.git"),
        ("https://tokenonly@github.com/a/b.git", "a/b.git"),
    ],
)
def test_embedded_cred_remote_is_recognized(url, path):
    m = _EMBEDDED_CRED_RE.match(url)
    assert m and m.group("path") == path


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a/b.git",   # plain HTTPS — handled by its own branch
        "git@github.com:a/b.git",       # SSH — handled by its own branch
        "https://user:pass@gitlab.com/a/b.git",  # not github, must not match
    ],
)
def test_embedded_cred_regex_does_not_overmatch(url):
    assert _EMBEDDED_CRED_RE.match(url) is None


# --- 2x "web-api has no loki_selector" (wrong estate) -----------------------

async def test_java_logs_rejects_unknown_app_without_touching_the_bastion():
    r = await jl.search(env="prod", app="not-an-app")
    assert not r.ok and r.error_code == "VALIDATION"
    assert "web-api" in r.user_message


async def test_java_logs_rejects_bad_env():
    r = await jl.search(env="uat", app="web-api")
    assert not r.ok and r.error_code == "VALIDATION"


async def test_java_logs_rejects_single_quote_before_the_round_trip():
    """The filter crosses an EC2 shell; a quote breaks quoting on the bastion."""
    r = await jl.search(env="prod", app="web-api", grep="a'b")
    assert not r.ok and r.error_code == "VALIDATION"


@pytest.mark.parametrize("bad", ["web-api.log; id", "../../etc/passwd", "a b.log", "$(id)"])
async def test_java_logs_rejects_injectable_file(bad):
    """`file` is interpolated unquoted into the command jlog.sh sends over ssh."""
    r = await jl.search(env="prod", app="web-api", file=bad)
    assert not r.ok and r.error_code == "VALIDATION"


async def test_java_logs_accepts_a_real_rotated_filename():
    """The allowlist must not reject the names `ls` actually returns."""
    assert jl._FILE_RE.match("web-api.log.3")
    assert jl._FILE_RE.match("localhost_access_log.2026-08-11.txt")


@pytest.mark.parametrize("bad", ["2026-08-11", "'''+__import__('os').system('id')+'''", "now-1h"])
async def test_java_logs_rejects_non_timestamp_kst(bad):
    """`kst` lands inside a python -c source in the wrapper."""
    r = await jl.search(env="prod", app="web-api", kst=bad)
    assert not r.ok and r.error_code == "VALIDATION"


async def test_java_logs_accepts_the_documented_kst_format():
    assert jl._KST_RE.match("2026-08-11 11:18")


async def test_java_logs_reports_missing_wrapper_as_config(monkeypatch):
    monkeypatch.setenv("JLOG_SCRIPT", "/nonexistent/jlog.sh")
    r = await jl.search(env="stag", app="web-api")
    assert not r.ok and r.error_code == "CONFIG"


def test_java_logs_apps_cover_every_tomcat_and_sidecar_log():
    assert {"web-admin", "web-api", "web-b2b", "web-b2c", "web-driver"} <= jl.APPS
    assert {"catalina", "access", "apl", "msg", "ls"} <= jl.APPS


def test_java_logs_wrapper_path_default_is_the_shared_skill():
    assert jl._DEFAULT_SCRIPT == (
        Path.home() / ".claude" / "skills" / "gogox-java-logs" / "jlog.sh"
    )


# --- 4x "Read: File content exceeds maximum allowed tokens" ------------------
# 2026-08-11: max_chars=80000 on one PR spilled the result to a single-line
# tool-results JSON, which Read's line-based offset/limit cannot shrink. Four
# identical failed Reads, then six `.{300}` Greps scraping the temp file.

async def test_pr_diff_caps_max_chars_above_the_ceiling(monkeypatch):
    from agentic.integrations import github as gh

    long_diff = "x" * 200_000

    class _R:
        text = long_diff
        def raise_for_status(self): pass

    class _C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _R()

    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda **k: _C())
    out = await gh.get_pr_diff(1, repo="gogovan/x", max_chars=80_000)
    assert len(out) < gh._MAX_DIFF_CHARS + 1000
    assert "capped at 40000" in out
    assert "Raising max_chars will not return more" in out


async def test_pr_diff_honours_a_smaller_request(monkeypatch):
    from agentic.integrations import github as gh

    class _R:
        text = "y" * 50_000
        def raise_for_status(self): pass

    class _C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _R()

    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda **k: _C())
    out = await gh.get_pr_diff(1, repo="gogovan/x", max_chars=5_000)
    assert "truncated at 5000 of 50000" in out
    assert "capped at" not in out, "no cap note when the caller asked for less"


def test_tool_descriptions_name_no_nonexistent_org():
    """`GoGoXTech/order-service` was an example org that 404s; the brain turned it
    into `org:gogox-tech` and got a 422."""
    import re
    from pathlib import Path as _P

    src = _P("src/agentic/sdk/mcp_tools.py").read_text()
    assert not re.search(r"(?i)gogox-?tech", src)
