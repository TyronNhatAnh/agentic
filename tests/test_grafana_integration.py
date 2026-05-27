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
