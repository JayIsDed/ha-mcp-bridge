"""Smoke tests for the HA client. Require HA_URL + HA_TOKEN in environment; skip otherwise."""

from __future__ import annotations

import os

import pytest

from ha_mcp_bridge.ha_client import HAClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("HA_TOKEN"),
    reason="HA_TOKEN not set; live-HA tests skipped",
)


@pytest.fixture
def ha_url() -> str:
    return os.environ.get("HA_URL", "http://192.168.8.127:8123")


@pytest.fixture
def ha_token() -> str:
    return os.environ["HA_TOKEN"]


async def test_list_states_returns_entities(ha_url: str, ha_token: str) -> None:
    async with HAClient(ha_url, ha_token) as ha:
        states = await ha.list_states()
    assert isinstance(states, list)
    assert len(states) > 0
    assert "entity_id" in states[0]


async def test_get_state_plant_shelf(ha_url: str, ha_token: str) -> None:
    async with HAClient(ha_url, ha_token) as ha:
        state = await ha.get_state("sensor.plant_shelf_temperatures_tank_center")
    # If ESPHome device is reachable this should return a dict; allow None until wired up.
    if state is not None:
        assert state["entity_id"] == "sensor.plant_shelf_temperatures_tank_center"
        assert "state" in state


async def test_history_shape(ha_url: str, ha_token: str) -> None:
    async with HAClient(ha_url, ha_token) as ha:
        history = await ha.get_history("sensor.plant_shelf_temperatures_tank_center", hours=1)
    assert isinstance(history, list)
    # Shape check — may be empty if entity hasn't changed in last hour, that's fine.
    if history:
        assert "state" in history[0]
