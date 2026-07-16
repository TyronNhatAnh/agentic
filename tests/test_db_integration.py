import os
import tempfile

os.environ.setdefault("AGENTIC_DB", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("AGENTIC_SERVICES_JSON", tempfile.mktemp(suffix=".json"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from agentic.config import settings  # noqa: E402
from agentic.integrations import db  # noqa: E402


# --- guard_sql (shared by staging + prod) ---------------------------------

def test_guard_rejects_mutation():
    safe, err = db.guard_sql("UPDATE orders SET x=1", row_cap=200)
    assert safe is None and err is not None and err.error_code == "VALIDATION"


def test_guard_rejects_multi_statement():
    safe, err = db.guard_sql("SELECT 1; DROP TABLE orders", row_cap=200)
    assert safe is None and err is not None and err.error_code == "VALIDATION"


def test_guard_appends_limit_to_bare_select():
    safe, err = db.guard_sql("SELECT id FROM orders", row_cap=50)
    assert err is None and safe.endswith("LIMIT 50")


# --- query_prod gating -----------------------------------------------------

async def test_query_prod_off_when_unconfigured(monkeypatch):
    """Empty prod base URL / token → CONFIG error, no network call."""
    monkeypatch.setattr(settings, "order_debug_prod_base_url", "", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_admin_token", "", raising=False)
    res = await db.query_prod("SELECT 1")
    assert res.error_code == "CONFIG"
    assert "ORDER_DEBUG_PROD_BASE_URL" in res.user_message


async def test_query_prod_targets_prod_host_and_token(monkeypatch):
    """query_prod must hit the prod base URL with the prod token + browser UA."""
    monkeypatch.setattr(settings, "order_debug_prod_base_url", "https://prod.example", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_admin_token", "PRODTOKEN", raising=False)
    seen = {}

    class _Resp:
        status_code = 200
        def json(self):  # noqa: D401
            return {"rowCount": 1, "rows": [{"id": 42}]}

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            seen["url"] = url
            seen["auth"] = headers["Authorization"]
            seen["ua"] = headers.get("User-Agent")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    res = await db.query_prod("SELECT id FROM orders WHERE id=42")
    assert res.ok
    assert seen["url"] == "https://prod.example/api/v1/admin/orders/debug/query"
    assert seen["auth"] == "Bearer PRODTOKEN"
    assert seen["ua"]  # browser UA sent so Cloudflare doesn't 1010 the call
    assert "42" in res.display()


# --- query_prod auto-login (no static token) -------------------------------

class _Resp:
    def __init__(self, status_code, *, cookies=None, payload=None, text=""):
        self.status_code = status_code
        self.cookies = cookies or {}
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_client_factory(handler):
    """Build an httpx.AsyncClient stand-in whose .post delegates to handler(url,
    data, json), so a test can branch on login vs query URL."""
    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, data=None, json=None):
            return handler(url, data, json)
    return _Client


def _configure_login(monkeypatch):
    monkeypatch.setattr(settings, "order_debug_prod_base_url", "https://prod.example", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_admin_token", "", raising=False)  # force login path
    monkeypatch.setattr(settings, "order_debug_prod_login_url", "https://biz.example/admin/login/menual", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_email", "me@x.com", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_pass", "secret", raising=False)
    monkeypatch.setattr(db, "_prod_token_cache", None, raising=False)


async def test_query_prod_logs_in_when_no_static_token(monkeypatch):
    _configure_login(monkeypatch)
    calls = []

    def handler(url, data, json):
        calls.append(url)
        if url.endswith("/login/menual"):
            assert data == {"email": "me@x.com", "pwd": "secret"}  # form fields
            return _Resp(302, cookies={"access_token": "MINTED"})
        return _Resp(200, payload={"rowCount": 1, "rows": [{"n": 1}]})

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    res = await db.query_prod("SELECT 1")
    assert res.ok
    assert any(u.endswith("/login/menual") for u in calls)
    assert db._prod_token_cache == "MINTED"


async def test_query_prod_relogins_on_auth_expiry(monkeypatch):
    _configure_login(monkeypatch)
    monkeypatch.setattr(db, "_prod_token_cache", "STALE", raising=False)  # pre-seed expired token
    state = {"query_calls": 0, "logins": 0}

    def handler(url, data, json):
        if url.endswith("/login/menual"):
            state["logins"] += 1
            return _Resp(302, cookies={"access_token": "FRESH"})
        state["query_calls"] += 1
        if state["query_calls"] == 1:
            return _Resp(401, text="token expired")  # stale token rejected
        return _Resp(200, payload={"rowCount": 0, "rows": []})

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    res = await db.query_prod("SELECT 1")
    assert res.ok
    assert state["logins"] == 1 and state["query_calls"] == 2
    assert db._prod_token_cache == "FRESH"


async def test_query_prod_login_bad_creds(monkeypatch):
    _configure_login(monkeypatch)

    def handler(url, data, json):
        # Login form re-renders (200, no cookie) on wrong creds.
        return _Resp(200, text="login page")

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(handler))
    res = await db.query_prod("SELECT 1")
    assert not res.ok and res.error_code == "AUTH"


async def test_query_prod_needs_login_config(monkeypatch):
    monkeypatch.setattr(settings, "order_debug_prod_base_url", "https://prod.example", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_admin_token", "", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_login_url", "", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_email", "", raising=False)
    monkeypatch.setattr(settings, "order_debug_prod_pass", "", raising=False)
    monkeypatch.setattr(db, "_prod_token_cache", None, raising=False)
    res = await db.query_prod("SELECT 1")
    assert not res.ok and res.error_code == "CONFIG"
