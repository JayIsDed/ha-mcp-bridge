"""A dark plug or probe must raise a flag.

sensor_stale skips unavailable/unknown entities and nothing else picked them
up outside the canopy board, so a heater plug or tank probe that went dark
produced no flag at all. These run the evaluators on a generic one-tank
registry (fixture entity ids), plus a check that the real registry marks the
life-support entries critical.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stand_in import healthy_shelf_states

from ha_mcp_bridge.shelf_flags import evaluate_all
from ha_mcp_bridge.shelf_registry import SHELF_ENTITIES

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

# One heated tank: two probes, a TDS probe, a thermostat and the smart plug it
# drives. Keys are the registry keys the evaluators look up.
REG = {
    "tank_center": {
        "entity_id": "sensor.tank_a_temp", "category": "thermal", "container": "tank_a",
        "role": "water_temp", "unit": "°F", "active": True, "target": 77.0, "band": 0.5,
        "flag_band": 1.0, "offline_level": "critical",
    },
    "tank_substrate": {
        "entity_id": "sensor.tank_a_temp_low", "category": "thermal", "container": "tank_a",
        "role": "water_temp", "unit": "°F", "active": True, "target": 77.0, "flag_band": 1.5,
        "offline_level": "critical",
    },
    "tds_tank": {
        "entity_id": "sensor.tank_a_tds", "category": "chemistry", "container": "tank_a",
        "role": "tds", "unit": "ppm", "active": True, "range": (200.0, 350.0),
    },
    "climate_main_tank": {
        "entity_id": "climate.tank_a", "category": "climate", "container": "tank_a",
        "role": "thermostat", "unit": None, "active": True, "target": 77.0,
        "offline_level": "critical",
    },
    "heater_power": {
        "entity_id": "sensor.heater_a_power", "category": "power", "container": "tank_a",
        "role": "heater_power", "unit": "W", "active": True, "range": (0.0, 150.0),
        "offline_level": "critical",
    },
    "heater_switch": {
        "entity_id": "switch.heater_a", "category": "power", "container": "tank_a",
        "role": "switch", "unit": None, "active": True, "offline_level": "critical",
    },
    "heater_voltage": {
        "entity_id": "sensor.heater_a_voltage", "category": "power", "container": "tank_a",
        "role": "voltage", "unit": "V", "active": True, "range": (115.0, 125.0),
    },
    "outlet_1_pump_switch": {
        "entity_id": "switch.pump_a", "category": "power", "container": "tank_a",
        "role": "switch", "unit": None, "active": True, "offline_level": "critical",
        "offline_after_s": 300,
    },
    "outlet_1_pump_power": {
        "entity_id": "sensor.pump_a_power", "category": "power", "container": "tank_a",
        "role": "outlet_power", "unit": "W", "active": True, "offline_level": "critical",
        "offline_after_s": 300,
    },
}


def st(eid: str, state: str, ago: timedelta = timedelta(0), attrs: dict | None = None) -> dict:
    ts = (NOW - ago).isoformat()
    return {"entity_id": eid, "state": state, "attributes": attrs or {},
            "last_changed": ts, "last_updated": ts, "last_reported": NOW.isoformat()}


def thermostat(action: str, mode: str = "heat", current: float = 77.0) -> dict:
    return st("climate.tank_a", mode, timedelta(days=30),
              {"hvac_action": action, "temperature": 77.0, "current_temperature": current})


def healthy() -> dict[str, dict]:
    """Heater calling and drawing, pump running, probes in band, TDS in range."""
    return {
        "sensor.tank_a_temp": st("sensor.tank_a_temp", "76.8"),
        "sensor.tank_a_temp_low": st("sensor.tank_a_temp_low", "76.7"),
        "sensor.tank_a_tds": st("sensor.tank_a_tds", "250"),
        "climate.tank_a": thermostat("heating"),
        "sensor.heater_a_power": st("sensor.heater_a_power", "100.0", timedelta(minutes=20)),
        "switch.heater_a": st("switch.heater_a", "on", timedelta(minutes=20)),
        "sensor.heater_a_voltage": st("sensor.heater_a_voltage", "121.0"),
        "switch.pump_a": st("switch.pump_a", "on", timedelta(days=30)),
        "sensor.pump_a_power": st("sensor.pump_a_power", "2.5", timedelta(seconds=10)),
    }


def flags_by_name(states: dict, registry: dict = REG) -> dict[str, dict]:
    return {f["flag"]: f for f in evaluate_all(states, registry, NOW)}


# ─── healthy control ─────────────────────────────────────────────────────────


def test_healthy_tank_is_clear() -> None:
    assert evaluate_all(healthy(), REG, NOW) == []


def test_healthy_idle_tank_is_clear() -> None:
    states = healthy()
    states["climate.tank_a"] = thermostat("idle")
    states["switch.heater_a"] = st("switch.heater_a", "off", timedelta(hours=2))
    states["sensor.heater_a_power"] = st("sensor.heater_a_power", "0.0", timedelta(hours=2))
    states["sensor.tank_a_temp"] = st("sensor.tank_a_temp", "77.2")
    assert evaluate_all(states, REG, NOW) == []


# ─── dark plug ───────────────────────────────────────────────────────────────


def test_dark_heater_plug_is_critical_before_the_tank_cools() -> None:
    """The plug drops off: every entity it exposes goes unavailable and the
    thermostat reports idle. The tank is still in band, so no breach fires yet;
    the offline flags are the only early warning.
    """
    states = healthy()
    for eid in ("switch.heater_a", "sensor.heater_a_power", "sensor.heater_a_voltage"):
        states[eid] = st(eid, "unavailable", timedelta(hours=3))
    states["climate.tank_a"] = thermostat("idle")
    flags = flags_by_name(states)
    assert flags["entity_offline:heater_switch"]["level"] == "critical"
    assert flags["entity_offline:heater_power"]["level"] == "critical"
    assert flags["entity_offline:heater_voltage"]["level"] == "warn"
    assert flags["entity_offline:heater_switch"]["since"] == (NOW - timedelta(hours=3)).isoformat()
    assert "3 h" in flags["entity_offline:heater_switch"]["message"]


def test_heater_missing_from_ha_flags_at_once() -> None:
    states = healthy()
    del states["switch.heater_a"]
    flags = flags_by_name(states)
    assert flags["entity_offline:heater_switch"]["level"] == "critical"
    assert flags["entity_offline:heater_switch"]["since"] is None


def test_cold_tank_with_dead_plug_flags_the_heater() -> None:
    states = healthy()
    for eid in ("switch.heater_a", "sensor.heater_a_power", "sensor.heater_a_voltage"):
        states[eid] = st(eid, "unavailable", timedelta(days=3))
    states["climate.tank_a"] = thermostat("idle", current=70.0)
    states["sensor.tank_a_temp"] = st("sensor.tank_a_temp", "70.0")
    states["sensor.tank_a_temp_low"] = st("sensor.tank_a_temp_low", "70.1")
    flag = flags_by_name(states)["tank_heater_not_heating"]
    assert flag["level"] == "critical"
    assert "unavailable" in flag["message"]
    assert "7.0°F below target" in flag["message"]


def test_heater_calling_without_power_is_critical() -> None:
    """Plug on, thermostat calling, heater draws nothing for 20 min."""
    states = healthy()
    states["sensor.heater_a_power"] = st("sensor.heater_a_power", "0.0", timedelta(minutes=20))
    flag = flags_by_name(states)["tank_heater_not_heating"]
    assert flag["level"] == "critical"
    assert "0.0 W" in flag["message"]


def test_heater_on_with_dark_power_reading_warns() -> None:
    states = healthy()
    states["sensor.heater_a_power"] = st("sensor.heater_a_power", "unavailable", timedelta(minutes=20))
    flags = flags_by_name(states)
    assert flags["tank_heater_not_heating"]["level"] == "warn"
    assert flags["entity_offline:heater_power"]["level"] == "critical"


def test_heater_just_switched_on_is_not_flagged() -> None:
    """0 W from a heater switched on 30 s ago is the meter catching up."""
    states = healthy()
    states["switch.heater_a"] = st("switch.heater_a", "on", timedelta(seconds=30))
    states["sensor.heater_a_power"] = st("sensor.heater_a_power", "0.0", timedelta(hours=2))
    assert "tank_heater_not_heating" not in flags_by_name(states)


def test_thermostat_off_is_not_a_dead_heater() -> None:
    states = healthy()
    states["climate.tank_a"] = thermostat("off", mode="off", current=74.0)
    states["sensor.tank_a_temp"] = st("sensor.tank_a_temp", "74.0")
    states["switch.heater_a"] = st("switch.heater_a", "off", timedelta(hours=2))
    states["sensor.heater_a_power"] = st("sensor.heater_a_power", "0.0", timedelta(hours=2))
    assert "tank_heater_not_heating" not in flags_by_name(states)


# ─── dark probes ─────────────────────────────────────────────────────────────


def test_dark_tank_probes_are_critical() -> None:
    states = healthy()
    states["sensor.tank_a_temp"] = st("sensor.tank_a_temp", "unavailable", timedelta(hours=1))
    states["sensor.tank_a_temp_low"] = st("sensor.tank_a_temp_low", "unknown", timedelta(hours=1))
    flags = flags_by_name(states)
    assert flags["entity_offline:tank_center"]["level"] == "critical"
    assert flags["entity_offline:tank_substrate"]["level"] == "critical"


def test_dark_tds_probe_warns() -> None:
    states = healthy()
    states["sensor.tank_a_tds"] = st("sensor.tank_a_tds", "unknown", timedelta(days=2))
    flag = flags_by_name(states)["entity_offline:tds_tank"]
    assert flag["level"] == "warn"
    assert "unknown" in flag["message"]


def test_null_state_counts_as_dark() -> None:
    states = healthy()
    states["sensor.tank_a_tds"] = st("sensor.tank_a_tds", None, timedelta(hours=1))  # type: ignore[arg-type]
    assert "entity_offline:tds_tank" in flags_by_name(states)


def test_brief_blip_does_not_flag() -> None:
    states = healthy()
    states["sensor.tank_a_temp"] = st("sensor.tank_a_temp", "unavailable", timedelta(seconds=30))
    assert not any(f.startswith("entity_offline") for f in flags_by_name(states))


# ─── the real registry ───────────────────────────────────────────────────────


def test_registry_marks_life_support_critical() -> None:
    for key in ("tank_center", "tank_substrate", "heater_switch", "heater_power",
                "climate_main_tank"):
        assert SHELF_ENTITIES[key].get("offline_level") == "critical", key


def test_real_registry_dark_heater_plug_flags() -> None:
    states = healthy_shelf_states(NOW)
    for key in ("heater_switch", "heater_power", "heater_voltage"):
        eid = SHELF_ENTITIES[key]["entity_id"]
        states[eid] = st(eid, "unavailable", timedelta(hours=3))
    flags = flags_by_name(states, SHELF_ENTITIES)
    assert flags["entity_offline:heater_switch"]["level"] == "critical"
    assert flags["entity_offline:heater_power"]["level"] == "critical"


# ─── heater: a normal cycle's tail is not a dead heater ──────────────────────
#
# The heater's own thermostat cuts out for up to ~5.7 min at the end of a
# normal cycle while the plug stays on and the thermostat still calls. The
# 5 min hold flagged that as a dead heater several times a day.


def tail(on_ago: timedelta, zero_ago: timedelta, tank: str = "77.3") -> dict[str, dict]:
    """Plug switched on `on_ago`, heater drew, then dropped to 0 W `zero_ago`."""
    states = healthy()
    states["switch.heater_a"] = st("switch.heater_a", "on", on_ago)
    states["sensor.heater_a_power"] = st("sensor.heater_a_power", "0.0", zero_ago)
    states["sensor.tank_a_temp"] = st("sensor.tank_a_temp", tank)
    states["sensor.tank_a_temp_low"] = st("sensor.tank_a_temp_low", tank)
    states["climate.tank_a"] = thermostat("heating", current=float(tank))
    return states


def test_normal_cycle_tail_is_not_flagged() -> None:
    """The longest tail seen in the measured history: 5.7 min at 0 W."""
    states = tail(timedelta(minutes=16), timedelta(minutes=5, seconds=40))
    assert "tank_heater_not_heating" not in flags_by_name(states)


def test_long_tail_inside_the_tail_hold_is_not_flagged() -> None:
    states = tail(timedelta(minutes=40), timedelta(minutes=14))
    assert "tank_heater_not_heating" not in flags_by_name(states)


def test_heater_that_dies_mid_cycle_is_caught_after_the_tail_hold() -> None:
    """Control: drew, then 0 W for 16 min with the thermostat calling."""
    states = tail(timedelta(minutes=40), timedelta(minutes=16))
    flag = flags_by_name(states)["tank_heater_not_heating"]
    assert flag["level"] == "critical"
    assert flag["since"] == (NOW - timedelta(minutes=16)).isoformat()


def test_heater_that_never_draws_is_caught_at_five_minutes() -> None:
    """Control: the plug switched on 6 min ago and the meter never moved off 0 W."""
    states = healthy()
    states["switch.heater_a"] = st("switch.heater_a", "on", timedelta(minutes=6))
    states["sensor.heater_a_power"] = st("sensor.heater_a_power", "0.0", timedelta(hours=2))
    flag = flags_by_name(states)["tank_heater_not_heating"]
    assert flag["level"] == "critical"
    assert flag["since"] == (NOW - timedelta(minutes=6)).isoformat()


def test_heater_that_stops_with_the_tank_cold_is_caught_at_five_minutes() -> None:
    """Control: a tail-shaped 0 W, but the tank is below the flag band."""
    states = tail(timedelta(minutes=40), timedelta(minutes=6), tank="75.5")
    assert flags_by_name(states)["tank_heater_not_heating"]["level"] == "critical"


# ─── pump: off, drawing nothing, or dark is critical ─────────────────────────


def test_pump_switched_off_is_critical() -> None:
    states = healthy()
    states["switch.pump_a"] = st("switch.pump_a", "off", timedelta(minutes=6))
    flag = flags_by_name(states)["pump_not_running"]
    assert flag["level"] == "critical"
    assert "switched off" in flag["message"]
    assert flag["since"] == (NOW - timedelta(minutes=6)).isoformat()


def test_pump_drawing_nothing_is_critical() -> None:
    """Outlet on, pump unplugged, seized or dead: the meter reads 0 W."""
    states = healthy()
    states["sensor.pump_a_power"] = st("sensor.pump_a_power", "0.0", timedelta(minutes=10))
    flag = flags_by_name(states)["pump_not_running"]
    assert flag["level"] == "critical"
    assert "0.0 W" in flag["message"]


def test_pump_dark_is_critical_after_five_minutes() -> None:
    states = healthy()
    for eid in ("switch.pump_a", "sensor.pump_a_power"):
        states[eid] = st(eid, "unavailable", timedelta(minutes=6))
    flags = flags_by_name(states)
    assert flags["entity_offline:outlet_1_pump_switch"]["level"] == "critical"
    assert "pump_not_running" not in flags  # one fault, reported by entity_offline


def test_pump_switched_on_a_moment_ago_is_not_flagged() -> None:
    """Control: 0 W from a pump switched on 30 s ago is the meter catching up."""
    states = healthy()
    states["switch.pump_a"] = st("switch.pump_a", "on", timedelta(seconds=30))
    states["sensor.pump_a_power"] = st("sensor.pump_a_power", "0.0", timedelta(hours=1))
    assert "pump_not_running" not in flags_by_name(states)


def test_pump_blip_under_five_minutes_is_not_flagged() -> None:
    """Control: a 2 min drop, about what an HA restart takes."""
    states = healthy()
    for eid in ("switch.pump_a", "sensor.pump_a_power"):
        states[eid] = st(eid, "unavailable", timedelta(minutes=2))
    assert not any("pump" in name for name in flags_by_name(states))


def test_registry_marks_the_pump_critical() -> None:
    for key in ("outlet_1_pump_switch", "outlet_1_pump_power"):
        assert SHELF_ENTITIES[key].get("offline_level") == "critical", key
        assert SHELF_ENTITIES[key].get("offline_after_s") == 300, key


def test_real_registry_pump_off_and_dark() -> None:
    states = healthy_shelf_states(NOW)
    eid = SHELF_ENTITIES["outlet_1_pump_switch"]["entity_id"]
    states[eid] = st(eid, "off", timedelta(minutes=6))
    assert flags_by_name(states, SHELF_ENTITIES)["pump_not_running"]["level"] == "critical"

    states = healthy_shelf_states(NOW)
    for key in ("outlet_1_pump_switch", "outlet_1_pump_power"):
        eid = SHELF_ENTITIES[key]["entity_id"]
        states[eid] = st(eid, "unavailable", timedelta(minutes=6))
    flags = flags_by_name(states, SHELF_ENTITIES)
    assert flags["entity_offline:outlet_1_pump_switch"]["level"] == "critical"
