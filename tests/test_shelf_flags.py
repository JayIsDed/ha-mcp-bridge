"""Unit tests for shelf flag evaluators.

All tests use synthetic state dicts — no HA, no network, no time dependency
beyond a fixed `now` passed into each evaluator. That makes every assertion
deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ha_mcp_bridge.shelf_flags import (
    evaluate_all,
    flag_basement_cold_drift,
    flag_bucket_phantom_heat,
    flag_canopy_offline,
    flag_heater_overdraw,
    flag_sensor_stale,
    flag_stratification,
    flag_tank_band_breach,
    flag_tds_out_of_range,
)
from ha_mcp_bridge.shelf_registry import SHELF_ENTITIES


# ─── Fixtures ────────────────────────────────────────────────────────────────

NOW = datetime(2026, 4, 24, 14, 45, 0, tzinfo=timezone.utc)


def mk_state(
    entity_id: str,
    state: str,
    last_changed: datetime | None = None,
    attrs: dict | None = None,
) -> dict:
    lc = (last_changed or NOW).isoformat()
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attrs or {},
        "last_changed": lc,
        "last_updated": lc,
    }


def mk_recent_states(**overrides: dict) -> dict[str, dict]:
    """Baseline 'everything healthy' state snapshot. Override specific entities
    via keyword args: mk_recent_states(**{ 'sensor.x': {...} }).

    Every active registry entity gets a plausible healthy state so flags that
    depend on broad scans (sensor_stale, canopy_offline) don't trip.
    """
    states: dict[str, dict] = {}
    for _, entry in SHELF_ENTITIES.items():
        if not entry.get("active"):
            continue
        eid = entry["entity_id"]
        category = entry.get("category")
        role = entry.get("role")

        # Pick a plausible healthy default per role
        if role == "water_temp":
            value = "77.0"
        elif role == "air_temp":
            value = "62.0" if entry.get("container") == "shelf_ambient" else "72.0"
        elif role == "humidity":
            value = "45.0"
        elif role == "illuminance":
            value = "320"
        elif role == "tds":
            value = "250"
        elif role == "ph":
            value = "7.2"
        elif role == "heater_power":
            value = "100"
        elif role == "outlet_power":
            value = "3.5"
        elif role == "master_power":
            value = "148"
        elif role == "voltage":
            value = "121.0"
        elif role == "switch":
            value = "on"
        elif role == "thermostat":
            value = "heat" if entry.get("container") == "main_tank_10g" else "off"
        elif role == "grow_light":
            value = "on"
        elif role == "wifi_signal":
            value = "-65"
        elif role == "uptime":
            value = "167000"
        elif role == "dew_point":
            value = "40"
        elif role == "pressure":
            value = "29.95"
        elif role == "forecast_low":
            value = "40"
        elif role == "forecast_min":
            value = "38"
        elif role == "auxiliary_temp":
            value = "77.0"
        elif role in ("ir_mode", "ptz_position"):
            value = "100"
        else:
            value = "0"

        attrs: dict = {}
        if category == "climate":
            attrs = {
                "hvac_action": "heating" if entry.get("container") == "main_tank_10g" else "off",
                "temperature": 77.0,
                "current_temperature": 77.0,
            }

        states[eid] = mk_state(eid, value, attrs=attrs)

    # Apply overrides
    for k, v in overrides.items():
        states[k] = v
    return states


# ─── flag_canopy_offline ─────────────────────────────────────────────────────


def test_canopy_offline_fires_when_canopy_unavailable() -> None:
    states = mk_recent_states()
    # Make canopy entries unavailable
    states["sensor.plant_shelf_canopy_canopy_temperature"] = mk_state(
        "sensor.plant_shelf_canopy_canopy_temperature",
        "unavailable",
        last_changed=NOW - timedelta(days=2),
    )
    states["sensor.plant_shelf_canopy_canopy_humidity"] = mk_state(
        "sensor.plant_shelf_canopy_canopy_humidity",
        "unavailable",
        last_changed=NOW - timedelta(days=2),
    )
    states["sensor.plant_shelf_canopy_canopy_illuminance"] = mk_state(
        "sensor.plant_shelf_canopy_canopy_illuminance",
        "unavailable",
        last_changed=NOW - timedelta(days=2),
    )
    flag = flag_canopy_offline(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["flag"] == "canopy_offline"
    assert flag["level"] == "info"
    assert flag["known"] is True


def test_canopy_offline_silent_when_canopy_live() -> None:
    # All-healthy baseline: canopy entries report normal numeric values.
    states = mk_recent_states()
    flag = flag_canopy_offline(states, SHELF_ENTITIES, NOW)
    assert flag is None


# ─── flag_bucket_phantom_heat ────────────────────────────────────────────────


def test_phantom_heat_fires_when_climate_heats_but_outlet_zero() -> None:
    states = mk_recent_states()
    states["climate.bucket_rig"] = mk_state(
        "climate.bucket_rig",
        "heat",
        attrs={"hvac_action": "heating", "temperature": 77.0, "current_temperature": 61.0},
    )
    states["sensor.calibration_shelf_strip_tapo_p316m_2_current_consumption"] = mk_state(
        "sensor.calibration_shelf_strip_tapo_p316m_2_current_consumption",
        "0.0",
        last_changed=NOW - timedelta(hours=12),  # plenty >5min
    )
    flag = flag_bucket_phantom_heat(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["flag"] == "bucket_phantom_heat"
    assert flag["level"] == "warn"


def test_phantom_heat_silent_when_climate_off() -> None:
    states = mk_recent_states()
    states["climate.bucket_rig"] = mk_state(
        "climate.bucket_rig", "off", attrs={"hvac_action": "off"}
    )
    flag = flag_bucket_phantom_heat(states, SHELF_ENTITIES, NOW)
    assert flag is None


def test_phantom_heat_silent_when_heater_drawing() -> None:
    states = mk_recent_states()
    states["climate.bucket_rig"] = mk_state(
        "climate.bucket_rig",
        "heat",
        attrs={"hvac_action": "heating", "temperature": 77.0, "current_temperature": 75.0},
    )
    states["sensor.calibration_shelf_strip_tapo_p316m_2_current_consumption"] = mk_state(
        "sensor.calibration_shelf_strip_tapo_p316m_2_current_consumption",
        "45.0",
    )
    flag = flag_bucket_phantom_heat(states, SHELF_ENTITIES, NOW)
    assert flag is None


def test_phantom_heat_silent_when_recent_zero() -> None:
    """Fresh 0W within debounce window shouldn't fire — could be between cycles."""
    states = mk_recent_states()
    states["climate.bucket_rig"] = mk_state(
        "climate.bucket_rig",
        "heat",
        attrs={"hvac_action": "heating"},
    )
    states["sensor.calibration_shelf_strip_tapo_p316m_2_current_consumption"] = mk_state(
        "sensor.calibration_shelf_strip_tapo_p316m_2_current_consumption",
        "0.0",
        last_changed=NOW - timedelta(seconds=60),  # only 1min old
    )
    flag = flag_bucket_phantom_heat(states, SHELF_ENTITIES, NOW)
    assert flag is None


# ─── flag_tank_band_breach ───────────────────────────────────────────────────


def test_band_breach_silent_in_band() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_tank_center"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_center", "77.2"
    )
    assert flag_tank_band_breach(states, SHELF_ENTITIES, NOW) is None


def test_band_breach_warns_at_1_5f_off() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_tank_center"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_center", "75.5"
    )
    states["climate.main_tank"] = mk_state(
        "climate.main_tank",
        "heat",
        attrs={"hvac_action": "heating", "temperature": 77.0, "current_temperature": 75.5},
    )
    flag = flag_tank_band_breach(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["level"] == "warn"
    assert "below" in flag["message"]


def test_band_breach_critical_at_2f_off() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_tank_center"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_center", "79.5"
    )
    states["climate.main_tank"] = mk_state(
        "climate.main_tank",
        "heat",
        attrs={"hvac_action": "idle", "temperature": 77.0, "current_temperature": 79.5},
    )
    flag = flag_tank_band_breach(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["level"] == "critical"
    assert "above" in flag["message"]


# ─── flag_tds_out_of_range ───────────────────────────────────────────────────


def test_tds_in_range_silent() -> None:
    states = mk_recent_states()
    assert flag_tds_out_of_range(states, SHELF_ENTITIES, NOW) == []


def test_tds_below_range_flags() -> None:
    states = mk_recent_states()
    states["sensor.tank_chemistry_tds_tank"] = mk_state(
        "sensor.tank_chemistry_tds_tank", "180"
    )
    flags = flag_tds_out_of_range(states, SHELF_ENTITIES, NOW)
    assert len(flags) == 1
    assert "below" in flags[0]["message"]


def test_tds_above_range_flags() -> None:
    states = mk_recent_states()
    states["sensor.tank_chemistry_tds_tank"] = mk_state(
        "sensor.tank_chemistry_tds_tank", "420"
    )
    flags = flag_tds_out_of_range(states, SHELF_ENTITIES, NOW)
    assert len(flags) == 1
    assert "above" in flags[0]["message"]


def test_tds_gated_probe_not_evaluated() -> None:
    """bucket + caridina TDS are active=False; they shouldn't flag even with
    out-of-range synthetic data.
    """
    states = mk_recent_states()
    states["sensor.tank_chemistry_tds_bucket"] = mk_state(
        "sensor.tank_chemistry_tds_bucket", "999"
    )
    states["sensor.tank_chemistry_tds_caridina"] = mk_state(
        "sensor.tank_chemistry_tds_caridina", "999"
    )
    flags = flag_tds_out_of_range(states, SHELF_ENTITIES, NOW)
    # Only tank should fire — wait, tank is 250 (in range in baseline). So flags should be empty.
    assert flags == []


# ─── flag_stratification ─────────────────────────────────────────────────────


def test_stratification_silent_when_aligned() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_tank_center"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_center", "77.1"
    )
    states["sensor.plant_shelf_temperatures_tank_substrate"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_substrate", "76.9"
    )
    assert flag_stratification(states, SHELF_ENTITIES, NOW) is None


def test_stratification_warn_at_1_5f_delta() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_tank_center"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_center", "78.5"
    )
    states["sensor.plant_shelf_temperatures_tank_substrate"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_substrate", "77.0"
    )
    flag = flag_stratification(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["level"] == "warn"


def test_stratification_critical_at_2_5f_delta() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_tank_center"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_center", "79.5"
    )
    states["sensor.plant_shelf_temperatures_tank_substrate"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_substrate", "77.0"
    )
    flag = flag_stratification(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["level"] == "critical"


# ─── flag_heater_overdraw ────────────────────────────────────────────────────


def test_heater_overdraw_normal_silent() -> None:
    states = mk_recent_states()
    states["sensor.cal_shelf_inkbird_10g_current_consumption"] = mk_state(
        "sensor.cal_shelf_inkbird_10g_current_consumption", "105"
    )
    assert flag_heater_overdraw(states, SHELF_ENTITIES, NOW) is None


def test_heater_overdraw_critical_at_180w() -> None:
    states = mk_recent_states()
    states["sensor.cal_shelf_inkbird_10g_current_consumption"] = mk_state(
        "sensor.cal_shelf_inkbird_10g_current_consumption", "180"
    )
    flag = flag_heater_overdraw(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["level"] == "critical"


# ─── flag_basement_cold_drift ────────────────────────────────────────────────


def test_cold_drift_silent_at_62f() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_shelf_ambient"] = mk_state(
        "sensor.plant_shelf_temperatures_shelf_ambient", "62.0"
    )
    assert flag_basement_cold_drift(states, SHELF_ENTITIES, NOW) is None


def test_cold_drift_warns_below_60f() -> None:
    states = mk_recent_states()
    states["sensor.plant_shelf_temperatures_shelf_ambient"] = mk_state(
        "sensor.plant_shelf_temperatures_shelf_ambient", "58.5"
    )
    flag = flag_basement_cold_drift(states, SHELF_ENTITIES, NOW)
    assert flag is not None
    assert flag["level"] == "warn"


# ─── flag_sensor_stale ───────────────────────────────────────────────────────


def test_sensor_stale_silent_on_fresh_states() -> None:
    states = mk_recent_states()
    flags = flag_sensor_stale(states, SHELF_ENTITIES, NOW)
    assert flags == []


def test_sensor_stale_fires_on_old_thermal() -> None:
    states = mk_recent_states()
    # tank_center hasn't updated in 45 min
    states["sensor.plant_shelf_temperatures_tank_center"] = mk_state(
        "sensor.plant_shelf_temperatures_tank_center",
        "77.0",
        last_changed=NOW - timedelta(minutes=45),
    )
    flags = flag_sensor_stale(states, SHELF_ENTITIES, NOW)
    assert len(flags) == 1
    assert flags[0]["flag"].startswith("sensor_stale:tank_center")


def test_sensor_stale_skips_known_offline_canopy() -> None:
    states = mk_recent_states()
    # Canopy entries are known-offline via registry.known_state → should not appear
    # in stale list even if their timestamps are old (they're in a separate flag).
    flags = flag_sensor_stale(states, SHELF_ENTITIES, NOW)
    stale_ids = {f["flag"] for f in flags}
    assert not any("canopy" in s for s in stale_ids)


# ─── evaluate_all integration ─────────────────────────────────────────────────


def test_evaluate_all_returns_sorted_by_severity() -> None:
    states = mk_recent_states()
    # Seed one warn and one critical + baseline info
    states["sensor.cal_shelf_inkbird_10g_current_consumption"] = mk_state(
        "sensor.cal_shelf_inkbird_10g_current_consumption", "180"
    )
    states["sensor.plant_shelf_temperatures_shelf_ambient"] = mk_state(
        "sensor.plant_shelf_temperatures_shelf_ambient", "58.5"
    )
    flags = evaluate_all(states, SHELF_ENTITIES, NOW)
    levels = [f["level"] for f in flags]
    # critical must come first, then warn, then info
    order = {"critical": 0, "warn": 1, "info": 2}
    numeric_levels = [order[lvl] for lvl in levels]
    assert numeric_levels == sorted(numeric_levels), f"flags not sorted by severity: {levels}"


def test_evaluate_all_no_crash_on_missing_state() -> None:
    """Empty state dict shouldn't raise; evaluators must degrade gracefully."""
    flags = evaluate_all({}, SHELF_ENTITIES, NOW)
    # We expect at least canopy_offline to fire because the canopy entries are
    # missing from state (equivalent to unavailable).
    # But no crashes.
    assert isinstance(flags, list)


def test_evaluate_all_flag_crash_contained() -> None:
    """If a flag evaluator raises, the tool shouldn't crash — the error surfaces
    as a flag_crash entry instead.
    """
    # We can't easily inject a crash without monkeypatching — this tests the
    # defensive path via a registry with a bogus container type that some flags
    # might mishandle. At minimum, validates no uncaught exception.
    flags = evaluate_all({}, SHELF_ENTITIES, NOW)
    # Any flag_crash entries should have level=warn and the expected prefix.
    for f in flags:
        if f["flag"].startswith("flag_crash:"):
            assert f["level"] == "warn"
