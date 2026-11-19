"""A stale archbox value must never read as current.

The pulse decided `online` from the power switch alone and showed whatever
number HA last held, so values months old read as live whenever the switch
was on. Every value now carries `as_of`; one older than STALE_AFTER_S is
held out of vitals, listed under `stale`, and flagged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stand_in import ha_state, healthy_archbox_states

from ha_mcp_bridge.archbox_health import ARCHBOX_ENTITIES, ARCHBOX_SWITCHES, compose_archbox

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(days=60)
TELEMETRY = [k for k in ARCHBOX_ENTITIES if k not in ("wall_power", "line_voltage",
                                                      "today_kwh", "month_kwh")]


def _age(states: dict, keys, when: datetime) -> dict:
    """Re-stamp the given keys' entities as last heard at `when`, values unchanged."""
    for key in keys:
        eid = ARCHBOX_ENTITIES[key]
        states[eid] = ha_state(eid, states[eid]["state"], when)
    return states


def test_fresh_values_carry_as_of_and_stay_clear() -> None:
    out = compose_archbox(healthy_archbox_states(NOW), NOW)
    assert out["summary"] == {"critical": 0, "warn": 0, "info": 0}
    assert out["stale"] == {}
    assert out["vitals"]["gpu_power"] == 31.2
    assert set(out["as_of"]) == set(ARCHBOX_ENTITIES) | set(ARCHBOX_SWITCHES)
    assert out["as_of"]["gpu_power"] == NOW.isoformat(timespec="seconds")


def test_stale_telemetry_while_on_is_flagged_and_not_current() -> None:
    """Switch on, telemetry from two months ago: nothing may read as live."""
    states = _age(healthy_archbox_states(NOW), TELEMETRY, LONG_AGO)
    out = compose_archbox(states, NOW)
    assert out["online"] is True
    for key in TELEMETRY:
        assert out["vitals"][key] is None, f"{key} shown as current"
        assert out["stale"][key]["as_of"] == LONG_AGO.isoformat(timespec="seconds")
    assert out["stale"]["gpu_power"]["value"] == 31.2
    flags = {f["flag"]: f for f in out["flags"]}
    for key in TELEMETRY:
        assert flags[f"sensor_stale:{key}"]["level"] == "warn"
    assert "60.0 days" in flags["sensor_stale:gpu_power"]["message"]
    assert not any(f.startswith("sensor_unavailable") for f in flags)
    # derived values must not be built from stale inputs
    assert out["vitals"]["non_gpu_power"] is None
    assert out["vitals"]["wall_power"] == 137.0


def test_a_value_just_past_the_threshold_is_stale() -> None:
    states = _age(healthy_archbox_states(NOW), ["gpu_temp"], NOW - timedelta(minutes=6))
    out = compose_archbox(states, NOW)
    assert out["vitals"]["gpu_temp"] is None
    assert "sensor_stale:gpu_temp" in {f["flag"] for f in out["flags"]}


def test_a_value_inside_the_threshold_is_current() -> None:
    states = _age(healthy_archbox_states(NOW), ["gpu_temp"], NOW - timedelta(minutes=2))
    out = compose_archbox(states, NOW)
    assert out["vitals"]["gpu_temp"] == 27.0
    assert out["flags"] == []


def test_stale_telemetry_while_off_is_held_out_without_a_warning() -> None:
    states = _age(healthy_archbox_states(NOW), TELEMETRY, LONG_AGO)
    eid = ARCHBOX_SWITCHES["power_switch"]
    states[eid] = ha_state(eid, "off", NOW)
    out = compose_archbox(states, NOW)
    assert out["vitals"]["cpu_temp"] is None
    assert "cpu_temp" in out["stale"]
    assert [f["flag"] for f in out["flags"]] == ["archbox_offline"]


def test_stale_plug_reading_warns_even_while_off() -> None:
    """The plug reports whether or not the box is on, so its silence is news."""
    states = _age(healthy_archbox_states(NOW), ["wall_power"], LONG_AGO)
    eid = ARCHBOX_SWITCHES["power_switch"]
    states[eid] = ha_state(eid, "off", NOW)
    out = compose_archbox(states, NOW)
    assert out["vitals"]["wall_power"] is None
    assert "sensor_stale:wall_power" in {f["flag"] for f in out["flags"]}


def test_value_without_a_timestamp_is_not_trusted() -> None:
    states = healthy_archbox_states(NOW)
    eid = ARCHBOX_ENTITIES["cpu_temp"]
    states[eid] = {"state": "36.5"}
    out = compose_archbox(states, NOW)
    assert out["vitals"]["cpu_temp"] is None
    assert "sensor_stale:cpu_temp" in {f["flag"] for f in out["flags"]}


def test_unreadable_power_switch_is_not_reported_as_asleep() -> None:
    states = healthy_archbox_states(NOW)
    eid = ARCHBOX_SWITCHES["power_switch"]
    states[eid] = ha_state(eid, "unavailable", NOW)
    flags = {f["flag"]: f for f in compose_archbox(states, NOW)["flags"]}
    assert flags["power_switch_unknown"]["level"] == "warn"
    assert "archbox_offline" not in flags
