"""Hermetic tests for git.prepare_read_workspace — no live git/network.

The tool must (a) fetch fresh remote state over HTTPS+token, (b) resolve the
target ref (default = latest releases/*, or a caller-supplied ref), and
(c) check that commit out into a dedicated `_reads` worktree, never touching the
main clone's branch. We mock `_run_git` so the test asserts the orchestration,
not real git behavior.
"""
from __future__ import annotations

import pytest

from agentic.config import settings
from agentic.integrations import git as g

pytestmark = pytest.mark.asyncio

_SHA = "4d371c723bed0d8b2416bcdc63963df29effe521"


def _setup(monkeypatch, tmp_path, run_git_impl):
    repo = tmp_path / "clone"
    (repo / ".git").mkdir(parents=True)
    svc = {"name": "svc", "repo_path": str(repo), "github_repo": "o/r"}

    monkeypatch.setattr(g, "resolve_service", lambda name: svc if name else None)
    monkeypatch.setattr(
        g, "resolve_service_by_github_repo",
        lambda repo_slug: svc if repo_slug == "o/r" else None,
    )
    monkeypatch.setattr(settings, "github_token", "tok")
    monkeypatch.setattr(settings, "worktree_dir", "")  # → repo/.worktrees/_reads/<ref>

    async def _authed(_path):
        return "https://x-access-token:tok@github.com/o/r", {}

    monkeypatch.setattr(g, "_authed_remote_url", _authed)
    monkeypatch.setattr(g, "_run_git", run_git_impl)
    return repo


async def test_default_picks_latest_release(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []

    async def fake_run_git(*args, cwd=None, env=None):
        calls.append(args)
        if args[0] == "fetch":
            return 0, "", ""
        if args[0] == "for-each-ref":
            return 0, f"origin/releases/DAPro-2.127\t{_SHA}", ""
        if args[0:2] == ("worktree", "add"):
            return 0, "", ""
        return 0, "", ""

    _setup(monkeypatch, tmp_path, fake_run_git)
    r = await g.prepare_read_workspace(service="svc")

    assert r.ok, r.error
    assert r.data["ref"] == "releases/DAPro-2.127"
    assert r.data["sha"] == _SHA
    assert "_reads" in r.data["read_path"]
    # fetched the releases/* refspec, not a bare fetch
    fetch = next(c for c in calls if c[0] == "fetch")
    assert "+refs/heads/releases/*:refs/remotes/origin/releases/*" in fetch
    # checked out DETACHED at the fresh sha (never touches the clone's branch)
    add = next(c for c in calls if c[0:2] == ("worktree", "add"))
    assert "--detach" in add and _SHA in add


async def test_explicit_ref_fetched_and_checked_out(monkeypatch, tmp_path):
    async def fake_run_git(*args, cwd=None, env=None):
        if args[0] == "fetch":
            # explicit ref refspec, not the releases glob
            assert "+refs/heads/master:refs/remotes/origin/master" in args
            return 0, "", ""
        if args[0] == "rev-parse":
            assert args[-1] == "refs/remotes/origin/master"
            return 0, _SHA, ""
        if args[0:2] == ("worktree", "add"):
            return 0, "", ""
        return 0, "", ""

    _setup(monkeypatch, tmp_path, fake_run_git)
    r = await g.prepare_read_workspace(service="svc", ref="master")

    assert r.ok, r.error
    assert r.data["ref"] == "master"
    assert r.data["sha"] == _SHA


async def test_resolves_by_repo_slug(monkeypatch, tmp_path):
    async def fake_run_git(*args, cwd=None, env=None):
        if args[0] == "for-each-ref":
            return 0, f"origin/releases/DAPro-2.9\t{_SHA}", ""
        return 0, "", ""

    _setup(monkeypatch, tmp_path, fake_run_git)
    r = await g.prepare_read_workspace(repo="o/r")
    assert r.ok, r.error
    assert r.data["ref"] == "releases/DAPro-2.9"


async def test_no_release_branch_errors(monkeypatch, tmp_path):
    async def fake_run_git(*args, cwd=None, env=None):
        if args[0] == "for-each-ref":
            return 0, "", ""  # no releases/* refs
        return 0, "", ""

    _setup(monkeypatch, tmp_path, fake_run_git)
    r = await g.prepare_read_workspace(service="svc")
    assert not r.ok
    assert r.error_code == "NOT_FOUND"


async def test_unknown_service_errors(monkeypatch, tmp_path):
    async def fake_run_git(*args, cwd=None, env=None):
        return 0, "", ""

    _setup(monkeypatch, tmp_path, fake_run_git)
    r = await g.prepare_read_workspace(service="")
    assert not r.ok
    assert r.error_code == "NOT_FOUND"


async def test_missing_token_errors(monkeypatch, tmp_path):
    async def fake_run_git(*args, cwd=None, env=None):
        return 0, "", ""

    _setup(monkeypatch, tmp_path, fake_run_git)
    monkeypatch.setattr(settings, "github_token", "")
    r = await g.prepare_read_workspace(service="svc")
    assert not r.ok
    assert r.error_code == "CONFIG"


async def test_missing_ref_surfaces_fetch_error(monkeypatch, tmp_path):
    async def fake_run_git(*args, cwd=None, env=None):
        if args[0] == "fetch":
            return 1, "", "fatal: couldn't find remote ref refs/heads/nope"
        return 0, "", ""

    _setup(monkeypatch, tmp_path, fake_run_git)
    r = await g.prepare_read_workspace(service="svc", ref="nope")
    assert not r.ok
    assert r.error_code == "GIT_FETCH"
