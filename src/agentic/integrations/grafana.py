"""Grafana integration — read-only Loki log search via the datasource proxy.

We hit Loki's *native* query API through Grafana's datasource proxy
(`/api/datasources/proxy/uid/<uid>/loki/api/v1/...`) rather than `/api/ds/query`,
because the native response is plain Loki JSON (`data.result[].values`) and parses
deterministically, while `/api/ds/query` returns columnar dataframes.

Two environments (stag/prod) each carry their own base URL + Loki datasource UID;
the `env` payload field selects which. Auth is a single SA basic-auth credential
(GRAFANA_SA_KR) shared across both instances.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import httpx

from ..config import settings
from ..store import resolve_service
from .result import ToolResult, classify_exception

# Loki caps; keep requests bounded so a broad query can't dump unbounded logs.
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50
# Per-line cap. Big enough to fit a typical JSON request log (so fields like
# user_id/hash aren't silently dropped) but still bounded.
_MAX_LINE_LEN = 1500


# env token → k8s namespace prefix used in the Loki `job` label (kr-<ns>/...).
# `dev` and `stag` both live on the nonprod Grafana instance; only the namespace
# differs, so they share credentials but substitute a different {env} token.
_ENV_ALIASES = {
    "stag": "stag", "staging": "stag", "nonprod": "stag", "non-prod": "stag",
    "dev": "dev", "develop": "dev", "development": "dev",
    "prod": "prod", "production": "prod", "kr": "prod",
}


def _norm_env(env: str | None) -> str:
    key = (env or "stag").strip().lower()
    if key not in _ENV_ALIASES:
        raise ValueError(f"env không hợp lệ: `{env}` (chỉ nhận dev/stag/prod)")
    return _ENV_ALIASES[key]


def _env_conf(env: str | None) -> tuple[str, str, str]:
    """Return (base_url, loki_uid, env_token) for the requested environment."""
    e = _norm_env(env)
    if e == "prod":
        base, uid = settings.grafana_prod_base_url, settings.grafana_prod_loki_uid
    else:  # stag + dev share the nonprod instance
        base, uid = settings.grafana_stag_base_url, settings.grafana_stag_loki_uid
    if not base or not (settings.grafana_sa_kr or "").strip():
        bucket = "prod" if e == "prod" else "stag"
        raise RuntimeError(
            f"Grafana {bucket} chưa cấu hình (GRAFANA_SA_KR / GRAFANA_*_BASE_URL)."
        )
    return base.rstrip("/"), uid, e


_REL_RE = re.compile(r"^now(?:-(\d+)([smhdw]))?$")
# Bare relative (no `now-` prefix), e.g. `20m`/`2h` — treated as "that long ago".
# The model (and users copying "20p") naturally emit this; accept it as now-<N><unit>.
_BARE_REL_RE = re.compile(r"^(\d+)([smhdw])$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _to_ns(expr: str) -> str:
    """Convert a time expression to a Loki-native epoch-nanosecond string.

    Loki's query_range does NOT accept Grafana relative syntax (`now-1h`); it wants
    epoch ns or RFC3339. We accept `now`, `now-<N><s|m|h|d|w>`, a bare relative
    `<N><s|m|h|d|w>` (treated as "ago"), RFC3339, or a raw epoch (seconds or ns)
    and normalize to ns.
    """
    s = (expr or "").strip()
    m = _REL_RE.match(s) or _BARE_REL_RE.match(s)
    if m:
        now_ns = time.time_ns()
        if not m.group(1):  # bare "now"
            return str(now_ns)
        delta_ns = int(m.group(1)) * _UNIT_S[m.group(2)] * 1_000_000_000
        return str(now_ns - delta_ns)
    if s.isdigit():  # raw epoch: 10-digit = seconds, else assume ns
        return str(int(s) * 1_000_000_000) if len(s) <= 11 else s
    # RFC3339 / ISO 8601 (accept trailing Z)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


_HEADERS = {"Accept": "application/json"}


def _basic_auth() -> httpx.BasicAuth | None:
    """SA basic-auth (one credential, works on both nonprod + prod-kr). `_env_conf`
    guarantees the credential is set before any request, so this returns None only
    in defensive paths."""
    sa = (settings.grafana_sa_kr or "").strip()
    return httpx.BasicAuth(settings.grafana_sa_user, sa) if sa else None


def _proxy_url(base: str, uid: str, path: str) -> str:
    return f"{base}/api/datasources/proxy/uid/{uid}{path}"


def _fmt_ts(ts_ns: str) -> str:
    """Render a Loki epoch-ns timestamp (UTC). Includes the date when the log is
    not from today so a follow-up trace can anchor its time window — otherwise a
    bare `HH:MM:SS` is ambiguous across days and the window has to be re-supplied."""
    try:
        dt = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
        fmt = "%H:%M:%S" if dt.date() == datetime.now(timezone.utc).date() else "%m-%d %H:%M:%S"
        return dt.strftime(fmt)
    except (ValueError, OverflowError):
        return "??:??:??"


def _format_streams(payload: dict, *, query: str, env: str, limit: int) -> str:
    data = payload.get("data") or {}
    result = data.get("result") or []
    # resultType "streams" = log lines; "matrix"/"vector" = metric (LogQL aggregation).
    if data.get("resultType") != "streams":
        return (
            f"_Query `{query}` ({env}) trả về `{data.get('resultType')}` "
            f"(metric/aggregation), không phải log lines._\n```{json.dumps(result)[:1500]}```"
        )

    entries: list[tuple[int, dict, str]] = []
    for stream in result:
        labels = stream.get("stream") or {}
        for ts_ns, line in stream.get("values") or []:
            try:
                entries.append((int(ts_ns), labels, line))
            except (ValueError, TypeError):
                continue
    if not entries:
        return f"_Không có log cho `{query}` ({env}) trong khoảng thời gian này._"

    entries.sort(key=lambda e: e[0], reverse=True)  # newest first
    shown = entries[:limit]
    head = (
        f"*Grafana/Loki `{env}`* — `{query}` "
        f"({len(entries)} dòng, hiển thị {len(shown)}):"
    )
    lines = [head]
    for ts_ns, labels, line in shown:
        svc = labels.get("app") or labels.get("service") or labels.get("container") or ""
        lvl = labels.get("level") or labels.get("detected_level") or ""
        prefix = " ".join(p for p in (_fmt_ts(ts_ns), lvl, svc) if p)
        body = line.strip()
        # Order/assign logs are big JSON; a silent cut makes a downstream summarizer
        # invent the fields it can't see (id/user_id/hash). Keep more, and mark the cut
        # so the model knows it's looking at a truncated line rather than the whole record.
        if len(body) > _MAX_LINE_LEN:
            body = body[:_MAX_LINE_LEN] + " …[truncated]"
        lines.append(f"`{prefix}` {body}")
    return "\n".join(lines)


def _resolve_query(query: str, service: str, log_filter: str) -> tuple[str | None, ToolResult | None]:
    """Build the effective LogQL query.

    Either a raw `query`, or a registered `service` whose `loki_selector` is the
    stream selector base, optionally narrowed by `log_filter` (e.g. `|= "ERROR"`).
    """
    if query and query.strip():
        return query.strip(), None
    if service and service.strip():
        svc = resolve_service(service.strip())
        if not svc:
            return None, ToolResult.failure(
                "NOT_FOUND",
                f"Không có service `{service}` trong registry. "
                "Kiểm tra AGENTIC_SERVICES_JSON hoặc đưa LogQL `query` trực tiếp.",
            )
        selector = (svc.get("loki_selector") or "").strip()
        if not selector:
            return None, ToolResult.failure(
                "CONFIG",
                f"Service `{svc['name']}` chưa cấu hình `loki_selector` trong registry.",
            )
        if log_filter and log_filter.strip():
            f = log_filter.strip()
            # The filter is concatenated raw onto the stream selector, so it must be
            # a valid LogQL line filter (`|=`/`|~`/`!=`/`!~ "..."`) or pipeline stage
            # (`| json`, `| level="error"`). A free-form search expression like
            # `level:error OR error OR "HTTP 500"` produces a cryptic Loki 400
            # ("unexpected IDENTIFIER"); reject it here with a syntax hint instead.
            if not f.startswith(("|=", "|~", "!=", "!~", "|")):
                return None, ToolResult.failure(
                    "VALIDATION",
                    f"`filter` phải là LogQL line filter, không phải biểu thức tìm kiếm. "
                    f"Nhận được: `{f[:120]}`.\n"
                    'Đúng: `|= "error"` · nhiều term AND: `|= "error" |= "500"` · '
                    'OR/case-insensitive: `|~ "(?i)error|exception|fatal|500"`. '
                    "LogQL không có toán tử `OR` hay cú pháp `level:error`.",
                )
            selector = f"{selector} {f}"
        return selector, None
    return None, ToolResult.failure(
        "VALIDATION", "Cần `query` (LogQL) hoặc `service` (tên service trong registry)."
    )


async def search_logs(
    query: str = "",
    env: str = "stag",
    since: str = "now-1h",
    until: str = "now",
    limit: int = _DEFAULT_LIMIT,
    direction: str = "backward",
    datasource_uid: str | None = None,
    service: str = "",
    log_filter: str = "",
) -> ToolResult:
    query, err = _resolve_query(query, service, log_filter)
    if err:
        return err
    base, uid, env_token = _env_conf(env)
    query = query.replace("{env}", env_token)  # selector templates use {env} → kr-<env>/...
    uid = (datasource_uid or uid or "").strip()
    if not uid:
        return ToolResult.failure(
            "CONFIG",
            f"Thiếu Loki datasource UID cho `{env}` "
            f"(set GRAFANA_{env.upper()}_LOKI_UID hoặc truyền `datasource_uid`). "
            "Dùng `grafana.list_datasources` để tìm UID.",
        )
    limit = max(1, min(int(limit), _MAX_LIMIT))
    try:
        start_ns, end_ns = _to_ns(since), _to_ns(until)
    except (ValueError, KeyError):
        return ToolResult.failure(
            "VALIDATION",
            f"Khoảng thời gian không hợp lệ (since=`{since}`, until=`{until}`). "
            "Dùng `now`, `now-15m`/`now-1h`/`now-24h`, hoặc RFC3339.",
        )
    params = {
        "query": query,
        "start": start_ns,
        "end": end_ns,
        "limit": str(limit),
        "direction": direction if direction in ("backward", "forward") else "backward",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            _proxy_url(base, uid, "/loki/api/v1/query_range"),
            headers=_HEADERS,
            params=params,
            auth=_basic_auth(),
        )
        if r.status_code == 400:
            detail = (r.json() or {}).get("message") if r.headers.get(
                "content-type", ""
            ).startswith("application/json") else r.text
            return ToolResult.failure(
                "VALIDATION", f"Loki từ chối query (LogQL sai?): {str(detail)[:300]}"
            )
        r.raise_for_status()
        payload = r.json()
    return ToolResult.success(_format_streams(payload, query=query, env=env, limit=limit))


async def list_datasources(env: str = "stag") -> ToolResult:
    base, _, _ = _env_conf(env)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{base}/api/datasources", headers=_HEADERS, auth=_basic_auth()
        )
        r.raise_for_status()
        items = r.json()
    if not items:
        return ToolResult.success(f"_Không có datasource nào trên Grafana `{env}`._")
    lines = [f"*Datasources Grafana `{env}`* ({len(items)}):"]
    for d in items:
        flag = " ⭐loki" if d.get("type") == "loki" else ""
        lines.append(f"• `{d.get('type')}` {d.get('name')} — uid `{d.get('uid')}`{flag}")
    return ToolResult.success("\n".join(lines))


# LogQL line filter that matches the common error/exception signatures. Kept in
# one place so search and the monitor count the same set of lines.
_ERROR_FILTER = '|~ "(?i)error|exception|fatal|panic|traceback|critical"'


async def count_errors(
    selector: str, env: str = "prod", window: str = "1h"
) -> int | None:
    """Count ERROR-ish Loki lines for a stream selector over `window`.

    Used by the background health monitor — not exposed as a brain tool. Runs a
    Loki *instant* metric query (`sum(count_over_time(<selector> <errfilter> [w]))`)
    so we get a single number, not a page of log lines. Returns the count, or
    None if the query failed / Grafana isn't configured (the caller surfaces the
    failure separately from a real zero).
    """
    try:
        base, uid, env_token = _env_conf(env)
    except (RuntimeError, ValueError):
        return None
    uid = (uid or "").strip()
    if not uid:
        return None
    selector = selector.replace("{env}", env_token)
    metric = f"sum(count_over_time({selector} {_ERROR_FILTER} [{window}]))"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                _proxy_url(base, uid, "/loki/api/v1/query"),
                headers=_HEADERS,
                params={"query": metric, "time": _to_ns("now")},
                auth=_basic_auth(),
            )
            r.raise_for_status()
            payload = r.json()
    except Exception:
        return None
    result = (payload.get("data") or {}).get("result") or []
    if not result:  # vector with no series = zero matches
        return 0
    try:
        return int(float(result[0]["value"][1]))
    except (KeyError, IndexError, ValueError, TypeError):
        return None


# ---------- dispatch ----------

ACTION_HANDLERS = {
    "grafana.search_logs": lambda p: search_logs(
        p.get("query", ""),
        p.get("env", "stag"),
        p.get("since", "now-1h"),
        p.get("until", "now"),
        p.get("limit", _DEFAULT_LIMIT),
        p.get("direction", "backward"),
        p.get("datasource_uid"),
        p.get("service", ""),
        p.get("filter", ""),
    ),
    "grafana.list_datasources": lambda p: list_datasources(p.get("env", "stag")),
}


async def execute_action(action_type: str, payload: dict) -> ToolResult:
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        return ToolResult.failure("UNKNOWN_ACTION", f"unknown action `{action_type}`")
    try:
        result = await handler(payload)
        if isinstance(result, ToolResult):
            return result
        return ToolResult.success(result)
    except Exception as e:
        return classify_exception(e, service="Grafana")
