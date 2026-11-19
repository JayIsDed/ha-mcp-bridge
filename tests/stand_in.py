"""A stand-in HA for the health-tool tests, plus healthy state generators.

The stand-in speaks just enough of HA's REST API (GET /api/states/<entity_id>)
for the health tools, so tests drive the real HAClient and the real tool
functions without a live HA.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiohttp import web

from ha_mcp_bridge.archbox_health import ARCHBOX_ENTITIES, ARCHBOX_SWITCHES
from ha_mcp_bridge.shelf_registry import SHELF_ENTITIES

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def ha_state(entity_id: str, state: str, at: datetime, attrs: dict | None = None) -> dict:
    """A state dict shaped like HA's REST payload."""
    ts = at.isoformat()
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attrs or {},
        "last_changed": ts,
        "last_updated": ts,
        "last_reported": ts,
    }


_SHELF_HEALTHY_BY_ROLE = {
    "water_temp": "77.0",
    "humidity": "45.0",
    "illuminance": "320",
    "tds": "250",
    "ph": "7.2",
    "heater_power": "100",
    "outlet_power": "3.5",
    "master_power": "148",
    "voltage": "121.0",
    "switch": "on",
    "grow_light": "on",
    "wifi_signal": "-65",
    "uptime": "167000",
    "dew_point": "40",
    "pressure": "29.95",
    "forecast_low": "40",
    "forecast_min": "38",
    "auxiliary_temp": "77.0",
    "ir_mode": "day",
    "ptz_position": "100",
}


def healthy_shelf_states(now: datetime) -> dict[str, dict]:
    """Every active shelf registry entity, reporting a healthy value at `now`."""
    states: dict[str, dict] = {}
    for entry in SHELF_ENTITIES.values():
        if not entry.get("active"):
            continue
        eid = entry["entity_id"]
        role = entry.get("role")
        attrs: dict[str, Any] = {}
        if role == "air_temp":
            value = "62.0"
        elif role == "thermostat":
            heating = entry.get("container") == "main_tank_10g"
            value = "heat" if heating else "off"
            attrs = {
                "hvac_action": "heating" if heating else "off",
                "temperature": 77.0,
                "current_temperature": 77.0,
            }
        else:
            value = _SHELF_HEALTHY_BY_ROLE.get(role, "0")
        states[eid] = ha_state(eid, value, now, attrs)
    return states


_ARCHBOX_HEALTHY = {
    "cpu_temp": "36.5", "gpu_temp": "27.0", "gpu_hotspot": "36.0", "gpu_vram": "36.0",
    "gpu_vrm": "32.2", "coolant_temp": "26.1", "nvme_temp": "40.9", "cpu_load": "0.4",
    "gpu_load": "0.0", "memory_used": "2.1", "gpu_power": "31.2", "wall_power": "137.0",
    "line_voltage": "121.3", "today_kwh": "0.255", "month_kwh": "105.5",
}


def healthy_archbox_states(now: datetime) -> dict[str, dict]:
    """Every archbox entity, powered on and reporting a healthy idle value at `now`."""
    states = {
        eid: ha_state(eid, _ARCHBOX_HEALTHY[key], now) for key, eid in ARCHBOX_ENTITIES.items()
    }
    for eid in ARCHBOX_SWITCHES.values():
        states[eid] = ha_state(eid, "on", now)
    return states


def states_handler(states: dict[str, dict]) -> Handler:
    """A stand-in HA that serves `states` and 404s anything else, like HA does."""

    async def handler(request: web.Request) -> web.StreamResponse:
        raw = states.get(request.match_info["entity_id"])
        if raw is None:
            return web.json_response({"message": "Entity not found."}, status=404)
        return web.json_response(raw)

    return handler
