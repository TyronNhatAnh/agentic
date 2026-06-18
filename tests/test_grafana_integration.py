import os
import tempfile

os.environ.setdefault("AGENTIC_DB", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("AGENTIC_SERVICES_JSON", tempfile.mktemp(suffix=".json"))

from agentic.integrations import grafana  # noqa: E402


def _streams_payload(line: str):
    return {
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"app": "order-service", "detected_level": "info"},
                    "values": [["1716711974000000000", line]],
                }
            ],
        }
    }


def test_long_line_is_marked_truncated():
    line = "PUT /orders/2717068/assign " + "x" * 5000 + ' user_id=400158'
    out = grafana._format_streams(
        _streams_payload(line), query="{job=\"x\"}", env="prod", limit=50
    )
    assert "…[truncated]" in out
    # the tail field beyond the cap must NOT survive — that's exactly what fed the hallucination
    assert "user_id=400158" not in out
    # body kept up to the cap (plus the marker), not the old 500-char cut
    assert len(out) > 500


def test_short_line_not_marked():
    out = grafana._format_streams(
        _streams_payload("PUT /orders/2717068/assign 200 OK"),
        query="{job=\"x\"}",
        env="prod",
        limit=50,
    )
    assert "…[truncated]" not in out
    assert "PUT /orders/2717068/assign 200 OK" in out


def test_short_field_within_cap_survives():
    line = "assign order=2717068 user_id=400158 status=200"
    out = grafana._format_streams(
        _streams_payload(line), query="{job=\"x\"}", env="prod", limit=50
    )
    assert "user_id=400158" in out
    assert "…[truncated]" not in out


def _multi_payload(n: int, base_ns: int = 1716711974000000000):
    # n rows, 1s apart, newest = base_ns + (n-1)s
    return {
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"app": "payment-service", "detected_level": "warn"},
                    "values": [[str(base_ns + i * 1_000_000_000), f"line {i}"] for i in range(n)],
                }
            ],
        }
    }


def test_capped_result_warns_and_shows_window():
    # exactly `limit` rows back => clamped; brain must be told it's truncated + the covered span
    out = grafana._format_streams(_multi_payload(5), query='{job="x"}', env="prod", limit=5)
    assert "Đạt cap 5 dòng" in out
    assert "phủ" in out and "→" in out  # covered window surfaced in header


def test_uncapped_result_no_warning():
    out = grafana._format_streams(_multi_payload(3), query='{job="x"}', env="prod", limit=50)
    assert "Đạt cap" not in out
    assert "phủ" in out  # window still shown, just no truncation warning


_SVC = {"name": "payment-service", "loki_selector": '{job="kr-{env}/argo-ggx-kr-payment-service"}'}


def test_freeform_filter_rejected_before_loki(monkeypatch):
    """A search-expression filter must fail fast with a syntax hint, not reach Loki."""
    monkeypatch.setattr(grafana, "resolve_service", lambda _: _SVC)
    q, err = grafana._resolve_query(
        "", "payment-service", 'level:error OR error OR "HTTP 500"'
    )
    assert q is None
    assert err is not None and err.error_code == "VALIDATION"
    assert "line filter" in err.user_message


def test_valid_line_filter_is_appended(monkeypatch):
    monkeypatch.setattr(grafana, "resolve_service", lambda _: _SVC)
    q, err = grafana._resolve_query("", "payment-service", '|= "error" |= "500"')
    assert err is None
    assert q == _SVC["loki_selector"] + ' |= "error" |= "500"'
