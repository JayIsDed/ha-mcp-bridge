"""Tests for shelf_health.compose_snapshot — the pure orchestrator half.

fetch_states / build_snapshot require a live HAClient; those are smoke-tested
elsewhere. This file covers the composition logic which is independent of I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ha_mcp_bridge.shelf_health import compose_snapshot
from ha_mcp_bridge.shelf_registry import SHELF_ENTITIES


NOW = datetime(2026, 4, 24, 14, 45, 0, tzinfo=timezone.utc)


def mk_state(eid: str, state: str, attrs: dict | None = None) -> dict:
    lc = NOW.isoformat()
    return {
        "entity_id": eid,
        "state": state,
        "attributes": attrs or {},
        "last_changed": lc,
        "last_updated": lc,
    }


def test_snapshot_has_required_top_level_keys() -> None:
    states = {}
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    assert "timestamp" in snap
    assert "summary" in snap
    assert "flags" in snap
    assert snap["summary"]["active_entities"] > 0


def test_snapshot_omits_empty_sections() -> None:
    """No active chemistry entries firing → response still returns chemistry dict
    for the active TDS entity but camera/light sections should exist given active
    entries. Verify sections only appear when populated by active registry entries.
    """
    snap = compose_snapshot({}, SHELF_ENTITIES, NOW)
    # Because no states are fetched, numeric sensors land in their categories as
    # 'not_fetched' rows. Section presence follows registry active membership.
    assert "thermal" in snap  # active thermal entries exist
    assert "chemistry" in snap  # tds_tank is active
    assert "camera" in snap  # several camera entities active


def test_snapshot_thermal_contains_tank_center() -> None:
    states = {
        "sensor.plant_shelf_temperatures_tank_center": mk_state(
            "sensor.plant_shelf_temperatures_tank_center", "77.3"
        ),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    assert "tank_center" in snap["thermal"]
    assert snap["thermal"]["tank_center"]["value"] == 77.3
    assert snap["thermal"]["tank_center"]["unit"] == "°F"
    assert snap["thermal"]["tank_center"]["target"] == 77.0
    assert snap["thermal"]["tank_center"]["delta"] == 0.3


def test_snapshot_climate_uses_climate_format() -> None:
    states = {
        "climate.main_tank": mk_state(
            "climate.main_tank",
            "heat",
            attrs={
                "hvac_action": "heating",
                "temperature": 77.0,
                "current_temperature": 77.3,
            },
        ),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    item = snap["climate"]["climate_main_tank"]
    assert item["mode"] == "heat"
    assert item["hvac_action"] == "heating"
    assert item["target"] == 77.0
    assert item["current"] == 77.3


def test_snapshot_light_has_brightness_pct() -> None:
    states = {
        "light.grow_white": mk_state(
            "light.grow_white", "on", attrs={"brightness": 61}
        ),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    item = snap["light"]["grow_white"]
    assert item["state"] == "on"
    assert item["brightness"] == 61
    assert item["brightness_pct"] == 24  # 61/255*100 rounded


def test_snapshot_switch_state_compacts_to_string() -> None:
    states = {
        "switch.cal_shelf_inkbird_10g": mk_state("switch.cal_shelf_inkbird_10g", "on"),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    assert snap["power"]["heater_switch"] == "on"


def test_snapshot_offline_counts_correct() -> None:
    states = {
        "sensor.plant_shelf_canopy_canopy_temperature": mk_state(
            "sensor.plant_shelf_canopy_canopy_temperature", "unavailable"
        ),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    assert snap["summary"]["offline"] >= 1


def test_snapshot_flags_section_populated() -> None:
    states = {
        "sensor.cal_shelf_inkbird_10g_current_consumption": mk_state(
            "sensor.cal_shelf_inkbird_10g_current_consumption", "200"
        ),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    flag_names = [f["flag"] for f in snap["flags"]]
    assert "heater_overdraw" in flag_names


def test_snapshot_range_status_populated_for_tds() -> None:
    states = {
        "sensor.tank_chemistry_tds_tank": mk_state(
            "sensor.tank_chemistry_tds_tank", "256"
        ),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    item = snap["chemistry"]["tds_tank"]
    assert item["value"] == 256.0
    assert item["range_status"] == "in"


def test_snapshot_range_status_flags_above() -> None:
    states = {
        "sensor.tank_chemistry_tds_tank": mk_state(
            "sensor.tank_chemistry_tds_tank", "420"
        ),
    }
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    item = snap["chemistry"]["tds_tank"]
    assert item["range_status"] == "above"


def test_snapshot_response_size_reasonable() -> None:
    """Full empty snapshot (no states fetched) should stay well under 20 KB so we
    have comfortable headroom under the 120 KB transport cap.
    """
    import json

    snap = compose_snapshot({}, SHELF_ENTITIES, NOW)
    size = len(json.dumps(snap, default=str))
    assert size < 20_000, f"empty snapshot is {size} bytes — check for payload bloat"


def test_snapshot_skips_inactive_entities() -> None:
    """Inactive registry entries (ph_tank, soil probes, tds_bucket/caridina) must
    not appear in the snapshot output.
    """
    states = {}
    snap = compose_snapshot(states, SHELF_ENTITIES, NOW)
    all_keys_in_output: set = set()
    for section in ("thermal", "chemistry", "power", "climate", "light", "camera", "weather", "infrastructure"):
        if section in snap:
            all_keys_in_output.update(snap[section].keys())
    # ph_tank + soil_mimosa_a|b + soil_basil + tds_bucket + tds_caridina are inactive
    inactive_keys = {"ph_tank", "soil_mimosa_a", "soil_mimosa_b", "soil_basil", "tds_bucket", "tds_caridina"}
    assert not (inactive_keys & all_keys_in_output), (
        f"inactive keys leaked into output: {inactive_keys & all_keys_in_output}"
    )
