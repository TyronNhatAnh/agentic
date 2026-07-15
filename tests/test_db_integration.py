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
