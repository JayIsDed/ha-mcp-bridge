"""Pure-function tests for the archbox pulse. No HA required."""

from datetime import datetime, timezone

from ha_mcp_bridge.archbox_health import (
    ARCHBOX_ENTITIES,
    ARCHBOX_SWITCHES,
    compose_archbox,
)

NOW = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)


def _states(**overrides):
    """Healthy idle baseline; override individual entities by key name."""
    base = {
        "sensor.archbox_cpu_temp": "36.5",
        "sensor.archbox_gpu_temp": "27.0",
        "sensor.archbox_gpu_hotspot": "36.0",
        "sensor.archbox_gpu_vram_temp": "36.0",
        "sensor.archbox_gpu_vrm_temp": "32.2",
        "sensor.archbox_coolant_temp": "26.1",
        "sensor.archbox_nvme_temp": "40.9",
        "sensor.archbox_cpu_load": "0.4",
        "sensor.archbox_gpu_load": "0.0",
        "sensor.archbox_memory_used": "2.1",
        "sensor.archbox_gpu_power": "31.2",
        "sensor.pc_strip_current_consumption": "137.0",
        "sensor.pc_strip_voltage": "121.3",
        "sensor.pc_strip_today_s_consumption": "0.255",
        "sensor.pc_strip_this_month_s_consumption": "105.5",
        "switch.archbox": "on",
        "switch.pc_strip": "on",
    }
    for key, val in overrides.items():
        eid = ARCHBOX_ENTITIES.get(key) or ARCHBOX_SWITCHES.get(key) or key
        base[eid] = val
    return {eid: {"state": val} for eid, val in base.items()}


def test_healthy_idle_has_no_flags():
    out = compose_archbox(_states(), NOW)
    assert out["online"] is True
    assert out["summary"] == {"critical": 0, "warn": 0, "info": 0}
    assert out["flags"] == []
    assert out["vitals"]["coolant_temp"] == 26.1


def test_derived_values():
    out = compose_archbox(_states(), NOW)
    v = out["vitals"]
    assert v["non_gpu_power"] == 105.8      # 137.0 - 31.2
    assert v["gpu_over_coolant"] == 0.9     # 27.0 - 26.1
    assert v["hotspot_delta"] == 9.0        # 36.0 - 27.0


def test_offline_is_info_not_warn():
    out = compose_archbox(_states(power_switch="off"), NOW)
    assert out["online"] is False
    flags = {f["flag"]: f for f in out["flags"]}
    assert flags["archbox_offline"]["level"] == "info"
    assert flags["archbox_offline"]["known"] is True


def test_missing_sensor_only_flags_when_online():
    off = compose_archbox(_states(power_switch="off", gpu_temp="unavailable"), NOW)
    assert not any(f["flag"].startswith("sensor_unavailable") for f in off["flags"])

    on = compose_archbox(_states(gpu_temp="unavailable"), NOW)
    assert any(f["flag"] == "sensor_unavailable:gpu_temp" for f in on["flags"])


def test_thermal_warn_and_critical():
    warn = compose_archbox(_states(gpu_temp="81.0"), NOW)
    assert any(f["flag"] == "gpu_temp_warn" for f in warn["flags"])
    assert warn["summary"]["warn"] >= 1

    crit = compose_archbox(_states(gpu_temp="88.0"), NOW)
    assert any(f["flag"] == "gpu_temp_critical" for f in crit["flags"])
    assert crit["summary"]["critical"] >= 1


def test_coolant_threshold_is_loop_specific():
    # 36C coolant would be fine as a die temp but is a warning for a water loop
    out = compose_archbox(_states(coolant_temp="36.0"), NOW)
    assert any(f["flag"] == "coolant_temp_warn" for f in out["flags"])


def test_hotspot_delta_flags_degraded_paste():
    out = compose_archbox(_states(gpu_temp="60.0", gpu_hotspot="90.0"), NOW)
    assert out["vitals"]["hotspot_delta"] == 30.0
    assert any(f["flag"] == "hotspot_delta_high" for f in out["flags"])


def test_mains_off_while_on_contradiction():
    out = compose_archbox(_states(mains_switch="off"), NOW)
    assert any(f["flag"] == "mains_off_while_on" for f in out["flags"])


def test_empty_states_does_not_explode():
    out = compose_archbox({}, NOW)
    assert out["online"] is False
    assert out["vitals"]["cpu_temp"] is None
    # offline, so unavailable sensors are not each flagged
    assert all(not f["flag"].startswith("sensor_unavailable") for f in out["flags"])
