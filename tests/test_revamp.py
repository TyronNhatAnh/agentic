"""Tests for the da-api revamp tier: policy routing, channel tool-scope gate,
Notion markdown converter + page creation chunking, and pipeline idempotency.

Hermetic — no live SDK/Notion/Slack. Notion HTTP is faked; the per-module SDK
one-shot is monkeypatched.
"""

from __future__ import annotations

import pytest

from agentic import policy
from agentic import revamp_pipeline as rp
from agentic.config import settings
from agentic.dispatcher import _revamp_scope
from agentic.integrations import notion as notion_int
from agentic.integrations.result import ToolResult
from agentic.sdk.permission import PendingPermissions, build_slack_permission_callback


# --------------------------------------------------------------------------- #
# policy routing
# --------------------------------------------------------------------------- #

def test_resolve_policy_prod_by_default(monkeypatch):
    monkeypatch.setattr(settings, "revamp_channel_id", "", raising=False)
    pol = policy.resolve_policy("C123")
    assert pol.name == "prod"
    assert pol.tool_scope is None
    assert pol.subagents is None  # all sub-agents


def test_resolve_policy_revamp_only_on_configured_channel(monkeypatch):
    monkeypatch.setattr(settings, "revamp_channel_id", "CREVAMP", raising=False)
    monkeypatch.setattr(settings, "revamp_legacy_repo", "/tmp/legacy", raising=False)
    assert policy.resolve_policy("CREVAMP").name == "revamp"
    assert policy.resolve_policy("COTHER").name == "prod"


def test_revamp_policy_is_full_capability(monkeypatch):
    # Phase discipline is prompt-held, not a hard gate: revamp runs the full
    # palette + all sub-agents, distinguished only by prompt + legacy repo root.
    monkeypatch.setattr(settings, "revamp_channel_id", "CREVAMP", raising=False)
    monkeypatch.setattr(settings, "revamp_legacy_repo", "/tmp/legacy", raising=False)
    pol = policy.resolve_policy("CREVAMP")
    assert pol.system_prompt == "brain_revamp"
    assert pol.tool_scope is None
    assert pol.subagents is None
    assert "/tmp/legacy" in pol.repo_roots


# --------------------------------------------------------------------------- #
# permission tool-scope gate (generic mechanism — reserved for future phased
# clamp; not applied to either tier today)
# --------------------------------------------------------------------------- #

# A representative read-only allow-set used only to exercise the gate.
_GATE_SCOPE = frozenset({"Read", "Glob", "Grep", "notion_create_page"})


def _cb(scope):
    return build_slack_permission_callback(
        pending=PendingPermissions(),
        slack_client=None,
        channel_id="C",
        thread_ts="t",
        tool_scope=scope,
    )


@pytest.mark.asyncio
async def test_scope_denies_out_of_scope_tool():
    cb = _cb(_GATE_SCOPE)
    res = await cb("Bash", {"command": "rm -rf /"}, None)
    assert res.behavior == "deny"
    res = await cb("mcp__agentic__jira_create_issue", {"summary": "x"}, None)
    assert res.behavior == "deny"


@pytest.mark.asyncio
async def test_scope_allows_in_scope_tool():
    cb = _cb(_GATE_SCOPE)
    res = await cb("Read", {"file_path": "/x"}, None)
    assert res.behavior == "allow"
    # MCP form resolves via bare suffix.
    res = await cb("mcp__agentic__notion_create_page", {"title": "x"}, None)
    assert res.behavior == "allow"


@pytest.mark.asyncio
async def test_no_scope_allows_everything():
    cb = _cb(None)  # prod default
    res = await cb("Bash", {"command": "go build"}, None)
    assert res.behavior == "allow"


# --------------------------------------------------------------------------- #
# Notion markdown → blocks
# --------------------------------------------------------------------------- #

def test_markdown_blocks_kinds():
    md = "# Title\n## Sub\npara line\n- item1\n1. step1\n```ruby\nx = 1\n```"
    blocks = notion_int.markdown_to_blocks(md)
    kinds = [b["type"] for b in blocks]
    assert kinds == [
        "heading_1", "heading_2", "paragraph",
        "bulleted_list_item", "numbered_list_item", "code",
    ]
    code = blocks[-1]["code"]
    assert code["language"] == "ruby"
    assert code["rich_text"][0]["text"]["content"] == "x = 1"


def test_markdown_rich_text_splits_long_content():
    long = "a" * 4500
    blocks = notion_int.markdown_to_blocks(long)
    rt = blocks[0]["paragraph"]["rich_text"]
    # 4500 chars → 3 segments capped at 2000 each.
    assert [len(s["text"]["content"]) for s in rt] == [2000, 2000, 500]


# --------------------------------------------------------------------------- #
# Notion create_page (faked HTTP) — child-block chunking
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, *a, **k):
        self.posts: list = []
        self.patches: list = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json):
        self.posts.append((url, json))
        return _FakeResp({"id": "pageid", "url": "https://notion.so/pageid"})

    async def patch(self, url, json):
        self.patches.append((url, json))
        return _FakeResp({})


@pytest.mark.asyncio
async def test_create_page_chunks_children_over_100(monkeypatch):
    monkeypatch.setattr(settings, "notion_token", "secret", raising=False)
    monkeypatch.setattr(settings, "notion_parent_page_id", "parent", raising=False)
    _FakeClient.instances.clear()
    monkeypatch.setattr(notion_int.httpx, "AsyncClient", _FakeClient)

    md = "\n".join(f"- item {i}" for i in range(250))  # 250 blocks
    res = await notion_int.create_page("Big page", md)
    assert res.ok
    assert res.data["url"] == "https://notion.so/pageid"

    client = _FakeClient.instances[-1]
    # POST carries the first 100; remaining 150 appended in 2 PATCH batches.
    assert len(client.posts[0][1]["children"]) == 100
    assert len(client.patches) == 2
    assert [len(p[1]["children"]) for p in client.patches] == [100, 50]


@pytest.mark.asyncio
async def test_create_page_requires_parent(monkeypatch):
    monkeypatch.setattr(settings, "notion_token", "secret", raising=False)
    monkeypatch.setattr(settings, "notion_parent_page_id", "", raising=False)
    with pytest.raises(RuntimeError):
        await notion_int.create_page("t", "body")


# --------------------------------------------------------------------------- #
# SCAN
# --------------------------------------------------------------------------- #

def test_scan_modules_lists_dirs_and_rb(tmp_path):
    scope = tmp_path / "app" / "services"
    scope.mkdir(parents=True)
    (scope / "order").mkdir()
    (scope / "payment.rb").write_text("class P; end")
    (scope / "notes.txt").write_text("skip me")
    (scope / ".hidden").mkdir()

    modules, dropped = rp._scan_modules(str(tmp_path), "app/services")
    assert dropped == 0
    assert sorted(modules) == ["app/services/order", "app/services/payment.rb"]


def test_scan_modules_cap(tmp_path, monkeypatch):
    scope = tmp_path / "m"
    scope.mkdir()
    for i in range(5):
        (scope / f"s{i}.rb").write_text("x")
    monkeypatch.setattr(settings, "revamp_module_cap", 3, raising=False)
    modules, dropped = rp._scan_modules(str(tmp_path), "m")
    assert len(modules) == 3
    assert dropped == 2


# --------------------------------------------------------------------------- #
# pipeline idempotency
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_pipeline_skips_done_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "revamp_legacy_repo", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "notion_token", "secret", raising=False)
    monkeypatch.setattr(settings, "notion_parent_page_id", "parent", raising=False)

    monkeypatch.setattr(rp, "_scan_modules", lambda repo, scope: (["mod_a", "mod_b"], 0))

    # mod_a already done; mod_b fresh.
    def fake_get(run_key, module):
        if module == "mod_a":
            return {"status": "done", "doc_url": "https://notion.so/old"}
        return None

    monkeypatch.setattr(rp, "get_revamp_module", fake_get)
    monkeypatch.setattr(rp, "upsert_revamp_module", lambda *a, **k: None)
    # No existing index → pipeline creates one.
    monkeypatch.setattr(rp, "get_revamp_run", lambda run_key: None)
    monkeypatch.setattr(rp, "upsert_revamp_run", lambda *a, **k: None)

    analysed: list[str] = []

    async def fake_oneshot(*, system_prompt, user_prompt, **k):
        # archaeologist prompts name the module; spec prompt mentions "SPEC"
        analysed.append(user_prompt)
        return "## doc\nVERIFIED: stuff"

    monkeypatch.setattr(rp, "_run_oneshot", fake_oneshot)

    created: list[dict] = []

    async def fake_create_page(title, markdown="", parent_id=None):
        created.append({"title": title, "parent_id": parent_id})
        return ToolResult.success({"url": "https://notion.so/new", "id": "IDX"})

    monkeypatch.setattr(rp.notion_int, "create_page", fake_create_page)

    summary = await rp.run_revamp_pipeline(
        scope="app", thread_ts="t", channel="c",
        slack_client=None, placeholder_ts=None,
    )

    titles = [c["title"] for c in created]
    # Index page created first; mod_b + SPEC nested under it (parent_id == index id).
    assert any("INDEX" in t for t in titles)
    nested = [c for c in created if "INDEX" not in c["title"]]
    assert nested and all(c["parent_id"] == "IDX" for c in nested)
    # mod_a never analysed (skipped); mod_b analysed; SPEC synthesised.
    assert not any("mod_a" in p for p in analysed)
    assert any("mod_b" in p for p in analysed)
    assert "♻️" in summary and "mod_a" in summary
    assert "✅" in summary and "mod_b" in summary
    assert any(t.endswith("mod_b") for t in titles)
    assert any("SPRINT SPEC" in t for t in titles)


@pytest.mark.asyncio
async def test_pipeline_reuses_existing_index(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "revamp_legacy_repo", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "notion_token", "secret", raising=False)
    monkeypatch.setattr(settings, "notion_parent_page_id", "parent", raising=False)
    monkeypatch.setattr(rp, "_scan_modules", lambda repo, scope: (["mod_x"], 0))
    monkeypatch.setattr(rp, "get_revamp_module", lambda rk, m: None)
    monkeypatch.setattr(rp, "upsert_revamp_module", lambda *a, **k: None)
    # Existing index → must NOT create a new one; nest under the existing id.
    monkeypatch.setattr(
        rp, "get_revamp_run",
        lambda rk: {"index_page_id": "OLD", "index_url": "https://notion.so/old"},
    )
    monkeypatch.setattr(rp, "upsert_revamp_run", lambda *a, **k: None)

    async def fake_oneshot(**k):
        return "doc"

    monkeypatch.setattr(rp, "_run_oneshot", fake_oneshot)

    created: list[dict] = []

    async def fake_create_page(title, markdown="", parent_id=None):
        created.append({"title": title, "parent_id": parent_id})
        return ToolResult.success({"url": "u", "id": "n"})

    monkeypatch.setattr(rp.notion_int, "create_page", fake_create_page)

    await rp.run_revamp_pipeline(
        scope="app", thread_ts="t", channel="c",
        slack_client=None, placeholder_ts=None,
    )

    assert not any("INDEX" in c["title"] for c in created)  # reused, not recreated
    assert all(c["parent_id"] == "OLD" for c in created)    # nested under existing


# --------------------------------------------------------------------------- #
# dispatcher command gating
# --------------------------------------------------------------------------- #

def test_revamp_scope_only_in_revamp_channel(monkeypatch):
    monkeypatch.setattr(settings, "revamp_channel_id", "CREVAMP", raising=False)
    assert _revamp_scope("revamp app/services/order", "CREVAMP") == "app/services/order"
    # Wrong channel → inert.
    assert _revamp_scope("revamp app/services", "COTHER") is None
    # Not a revamp command.
    assert _revamp_scope("phân tích giúp tôi", "CREVAMP") is None


def test_revamp_scope_disabled_without_config(monkeypatch):
    monkeypatch.setattr(settings, "revamp_channel_id", "", raising=False)
    assert _revamp_scope("revamp x", "CREVAMP") is None
