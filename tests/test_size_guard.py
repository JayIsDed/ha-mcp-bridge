"""Unit tests for the response-size guard helpers.

No live HA required — these verify the guard boundary logic directly.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def server_mod(monkeypatch):
    """Re-import server with a tiny cap so we can exercise the guard cheaply."""
    monkeypatch.setenv("HA_MCP_MAX_RESPONSE_BYTES", "500")
    monkeypatch.setenv("HA_TOKEN", "fake-for-import")
    import ha_mcp_bridge.server as mod

    importlib.reload(mod)
    yield mod
    # Restore default cap on exit.
    monkeypatch.delenv("HA_MCP_MAX_RESPONSE_BYTES", raising=False)
    importlib.reload(mod)


def test_guard_dict_passes_small_payload(server_mod) -> None:
    out = server_mod._guard_dict("demo", {"ok": True, "n": 1}, "hint")
    assert out == {"ok": True, "n": 1}


def test_guard_dict_trips_on_oversize(server_mod) -> None:
    big = {"rows": [{"x": "y" * 50} for _ in range(20)]}
    out = server_mod._guard_dict("demo", big, "try smaller")
    assert out["error"] == "response_too_large"
    assert out["tool"] == "demo"
    assert out["size_bytes"] > 500
    assert out["limit_bytes"] == 500
    assert out["hint"] == "try smaller"


def test_guard_list_passes_small_payload(server_mod) -> None:
    out = server_mod._guard_list("demo", [{"a": 1}, {"a": 2}], "hint")
    assert out == [{"a": 1}, {"a": 2}]


def test_guard_list_trips_on_oversize(server_mod) -> None:
    big = [{"state": "x" * 100} for _ in range(20)]
    out = server_mod._guard_list("demo", big, "try smaller")
    assert len(out) == 1
    assert out[0]["error"] == "response_too_large"
    assert out[0]["tool"] == "demo"
    assert out[0]["hint"] == "try smaller"
