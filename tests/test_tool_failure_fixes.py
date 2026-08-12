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
