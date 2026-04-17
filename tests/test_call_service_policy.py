"""Policy tests for the scoped ha_call_service allowlist.

No live HA required — denial paths short-circuit before any HTTP call, and the one
allow-path test monkeypatches HAClient.call_service so the tool returns a mocked
success without touching the network.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def server_mod(monkeypatch):
    """Re-import server with a minimal allowlist set."""
    monkeypatch.setenv("HA_MCP_ALLOWED_SERVICES", "light.*,switch.turn_on,scene.turn_on")
    monkeypatch.setenv("HA_MCP_ALLOWED_ENTITY_PATTERNS", "*plant_shelf*,light.grow_*")
    monkeypatch.setenv("HA_TOKEN", "fake-for-import")
    import ha_mcp_bridge.server as mod

    importlib.reload(mod)
    yield mod
    monkeypatch.delenv("HA_MCP_ALLOWED_SERVICES", raising=False)
    monkeypatch.delenv("HA_MCP_ALLOWED_ENTITY_PATTERNS", raising=False)
    importlib.reload(mod)


@pytest.fixture
def server_disabled(monkeypatch):
    """Re-import with allowlists unset — tool should default-deny."""
    monkeypatch.delenv("HA_MCP_ALLOWED_SERVICES", raising=False)
    monkeypatch.delenv("HA_MCP_ALLOWED_ENTITY_PATTERNS", raising=False)
    monkeypatch.setenv("HA_TOKEN", "fake-for-import")
    import ha_mcp_bridge.server as mod

    importlib.reload(mod)
    yield mod
    importlib.reload(mod)


async def test_disabled_without_env(server_disabled) -> None:
    out = await server_disabled.ha_call_service("light", "turn_on", {"entity_id": "light.anything"})
    assert out["error"] == "ha_call_service disabled"
    assert out["allowed_services_set"] is False
    assert out["allowed_entity_patterns_set"] is False


async def test_service_not_in_allowlist(server_mod) -> None:
    out = await server_mod.ha_call_service(
        "homeassistant", "restart", {"entity_id": "light.grow_main"}
    )
    assert out["error"] == "service_not_allowed"
    assert out["service"] == "homeassistant.restart"


async def test_entity_not_in_allowlist(server_mod) -> None:
    out = await server_mod.ha_call_service(
        "light", "turn_on", {"entity_id": "light.bedroom"}
    )
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "light.bedroom"


async def test_missing_entity_id(server_mod) -> None:
    out = await server_mod.ha_call_service("light", "turn_on", {"brightness_pct": 50})
    assert out["error"] == "entity_required"


async def test_area_targeting_blocked(server_mod) -> None:
    out = await server_mod.ha_call_service(
        "light", "turn_on", {"area_id": "plant_shelf"}
    )
    assert out["error"] == "entity_required"
    assert "Area/device" in out["hint"]


async def test_list_entity_partial_denied(server_mod) -> None:
    out = await server_mod.ha_call_service(
        "light",
        "turn_on",
        {"entity_id": ["light.grow_main", "light.bedroom"]},
    )
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "light.bedroom"


async def test_wildcard_service_matches(server_mod, monkeypatch) -> None:
    """light.turn_off should pass via the 'light.*' glob."""
    called: dict = {}

    class _FakeHA:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def call_service(self, domain, service, data):
            called["args"] = (domain, service, data)
            return [{"entity_id": data["entity_id"], "state": "off"}]

    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA())

    out = await server_mod.ha_call_service(
        "light", "turn_off", {"entity_id": "light.grow_main"}
    )
    assert out["ok"] is True
    assert out["service"] == "light.turn_off"
    assert called["args"] == ("light", "turn_off", {"entity_id": "light.grow_main"})


async def test_wildcard_entity_matches(server_mod, monkeypatch) -> None:
    """`*plant_shelf*` should match `switch.plant_shelf_heater`."""
    called: dict = {}

    class _FakeHA:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def call_service(self, domain, service, data):
            called["ok"] = True
            return []

    monkeypatch.setattr(server_mod, "_client", lambda: _FakeHA())

    out = await server_mod.ha_call_service(
        "switch", "turn_on", {"entity_id": "switch.plant_shelf_heater"}
    )
    assert out["ok"] is True
    assert called["ok"] is True
