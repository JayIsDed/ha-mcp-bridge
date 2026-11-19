"""A pulse that cannot see HA must say so.

HA down, HA hung, or the token rejected: every state read fails, and the
health tools must return {"error": ...}. Before this, fetch_states swallowed
each failure, the evaluators saw an empty snapshot, and the pulse came back
all clear. Drives the real tool functions through the real HAClient.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timezone

import pytest
from aiohttp import web
from stand_in import healthy_archbox_states, healthy_shelf_states, states_handler

PULSES = ("shelf_pulse", "shelf_health_full", "archbox_pulse")


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "stand-in-token")
    import ha_mcp_bridge.server as mod

    monkeypatch.setattr(mod, "HA_TOKEN", "stand-in-token")
    monkeypatch.setattr(mod, "HA_TIMEOUT", 2.0)
    return mod


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("tool", PULSES)
async def test_ha_down_is_an_error(server, monkeypatch, tool) -> None:
    monkeypatch.setattr(server, "HA_URL", f"http://127.0.0.1:{_closed_port()}")
    out = await getattr(server, tool)()
    assert "error" in out, f"{tool} reported health with HA unreachable: {out}"
    assert "flags" not in out


@pytest.mark.parametrize("tool", PULSES)
async def test_rejected_token_is_an_error(server, monkeypatch, stand_in_ha, tool) -> None:
    async def unauthorized(request: web.Request) -> web.StreamResponse:
        return web.json_response({"message": "Invalid access token"}, status=401)

    monkeypatch.setattr(server, "HA_URL", await stand_in_ha(unauthorized))
    out = await getattr(server, tool)()
    assert "error" in out, f"{tool} reported health with the token rejected: {out}"
    assert "Unauthorized" in out["error"]


@pytest.mark.parametrize("tool", PULSES)
async def test_ha_hung_is_an_error(server, monkeypatch, stand_in_ha, tool) -> None:
    async def hang(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(3)
        return web.json_response({})

    monkeypatch.setattr(server, "HA_TIMEOUT", 0.3)
    monkeypatch.setattr(server, "HA_URL", await stand_in_ha(hang))
    out = await getattr(server, tool)()
    assert "error" in out, f"{tool} reported health with HA not answering: {out}"


async def test_healthy_ha_stays_clear(server, monkeypatch, stand_in_ha) -> None:
    now = datetime.now(timezone.utc)
    states = {**healthy_shelf_states(now), **healthy_archbox_states(now)}
    monkeypatch.setattr(server, "HA_URL", await stand_in_ha(states_handler(states)))

    shelf = await server.shelf_pulse()
    assert "error" not in shelf
    assert shelf["summary"] == {"critical": 0, "warn": 0, "info": 0}, shelf["flags"]
    assert shelf["vitals"]["tank_center"] == 77.0

    box = await server.archbox_pulse()
    assert "error" not in box
    assert box["online"] is True
    assert box["summary"] == {"critical": 0, "warn": 0, "info": 0}, box["flags"]
