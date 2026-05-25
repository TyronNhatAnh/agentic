import asyncio

from agentic.agents import base


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"ok", b""


async def test_run_claude_uses_neutral_runtime_dir_by_default(monkeypatch, tmp_path):
    seen = {}
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(base.settings, "claude_runtime_dir", str(runtime_dir))

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    out = await base.run_claude("system", "user")

    assert out == "ok"
    assert seen["cwd"] == str(runtime_dir)
    assert runtime_dir.is_dir()


async def test_run_claude_keeps_explicit_repo_cwd(monkeypatch, tmp_path):
    seen = {}
    repo_dir = tmp_path / "repo"
    monkeypatch.setattr(base.settings, "claude_runtime_dir", str(tmp_path / "runtime"))

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await base.run_claude("system", "user", cwd=str(repo_dir))

    assert seen["cwd"] == str(repo_dir)
    assert repo_dir.is_dir()


async def test_run_claude_can_enable_accept_edits(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(base.settings, "claude_runtime_dir", str(tmp_path / "runtime"))

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["args"] = args
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await base.run_claude("system", "user", permission_mode="acceptEdits")

    assert "--permission-mode" in seen["args"]
    assert "acceptEdits" in seen["args"]
