import httpx
import pytest

import agentic.integrations.jira as jira
from agentic.integrations.jira import _adf_to_text, _issue_key, get_comments


def test_adf_to_text_extracts_description_specs():
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "content": [{"type": "text", "text": "Specs"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Driver lookup by user ID."}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "TypeCD must be driver"}],
                            }
                        ],
                    }
                ],
            },
        ],
    }

    out = _adf_to_text(adf)

    assert "Specs" in out
    assert "Driver lookup by user ID." in out
    assert "- TypeCD must be driver" in out


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("KRP-123", "KRP-123"),
        ("krp-123", "KRP-123"),
        ("https://gogox.atlassian.net/browse/KRP-123", "KRP-123"),
        ("please review KRP-7 today", "KRP-7"),
        ("no key here", "no key here"),
    ],
)
def test_issue_key_normalizes_url_and_bare(raw, expected):
    assert _issue_key(raw) == expected


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, captured):
        self._payload = payload
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self._captured["url"] = url
        self._captured["params"] = params
        return _FakeResp(self._payload)


@pytest.mark.asyncio
async def test_get_comments_renders_last_oldest_to_newest(monkeypatch):
    monkeypatch.setattr(jira, "_base", lambda: "https://x.atlassian.net")
    # API returns newest-first (orderBy=-created); rendering must reverse to oldest→newest.
    payload = {
        "total": 7,
        "comments": [
            {"author": {"displayName": "Bob"}, "created": "2026-06-18T03:00:00.000+0000",
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "newer"}]}]}},
            {"author": {"displayName": "Alice"}, "created": "2026-06-17T03:00:00.000+0000",
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "older"}]}]}},
        ],
    }
    captured: dict = {}
    monkeypatch.setattr(jira, "_client", lambda: _FakeClient(payload, captured))

    res = await get_comments("https://x.atlassian.net/browse/KRP-9", limit=5)

    assert res.ok
    # URL was normalized to the bare key.
    assert captured["url"].endswith("/issue/KRP-9/comment")
    assert captured["params"] == {"orderBy": "-created", "maxResults": 5}
    text = res.data
    assert text.index("older") < text.index("newer")  # oldest first
    assert "Alice" in text and "Bob" in text
    assert "(total 7)" in text  # total > shown


@pytest.mark.asyncio
async def test_get_comments_empty(monkeypatch):
    monkeypatch.setattr(jira, "_base", lambda: "https://x.atlassian.net")
    monkeypatch.setattr(jira, "_client", lambda: _FakeClient({"total": 0, "comments": []}, {}))
    res = await get_comments("KRP-1")
    assert res.ok
    assert "no comments yet" in res.data
