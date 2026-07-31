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


# --------------------------------------------------------------------------
# full-suite composer
# --------------------------------------------------------------------------

from ha_mcp_bridge.archbox_health import compose_archbox_full


def _row(measurement, field, value, **tags):
    r = {"_measurement": measurement, "_field": field, "_value": value}
    r.update(tags)
    return r


def _full_rows():
    rows = [
        _row("temp", "temp", 36.5, sensor="k10temp_tctl"),
        _row("temp", "temp", 30.1, sensor="k10temp_tccd1"),
        _row("temp", "temp", 31.9, sensor="k10temp_tccd2"),
        _row("temp", "temp", 34.5, sensor="nct6686_thermistor_14"),
        _row("temp", "temp", 48.0, sensor="r8169_0_4d00:00"),
        _row("temp", "temp", 33.0, sensor="amdgpu_edge"),
        _row("icx3_temp", "temp", 36.0, sensor="hotspot"),
        _row("icx3_temp", "temp", 28.5, sensor="mem1"),
        _row("icx3_temp", "temp", 36.0, sensor="vram"),
        _row("icx3_fan", "rpm", 1100, fan="0"),
        _row("nvidia_smi", "temperature_gpu", 27.0, pstate="P8"),
        _row("nvidia_smi", "power_draw", 30.4, pstate="P8"),
        _row("prometheus", "coolercontrol_temperature_celsius", 26.1, sensor="temp0"),
        _row("prometheus", "coolercontrol_fan_rpm", 1076, channel="fan1"),
        _row("prometheus", "coolercontrol_fan_rpm", 0, channel="fan2"),
        _row("cpu", "usage_active", 0.4, cpu="cpu-total"),
        _row("mem", "used_percent", 2.2),
        _row("disk", "used_percent", 39.8, path="/srv/ai-models"),
    ]
    # the whole point: four DRIVES and four DIMMS as distinct devices
    for i, t in enumerate([41.85, 40.85, 40.85, 41.85]):
        rows.append(_row("temp_detail", "temp", t, chip="nvme", dev=f"nvme{i}", label="Composite"))
    for addr, t in zip(("9-0050", "9-0051", "9-0052", "9-0053"), (38.0, 38.75, 38.75, 37.5)):
        rows.append(_row("temp_detail", "temp", t, chip="spd5118", dev=addr, label="temp1"))
    return rows


def _ha_states():
    return {
        "switch.archbox": {"state": "on"},
        "switch.pc_strip": {"state": "on"},
        "sensor.pc_strip_current_consumption": {"state": "136.4"},
        "sensor.pc_strip_voltage": {"state": "121.1"},
        "sensor.pc_strip_today_s_consumption": {"state": "0.3"},
        "sensor.pc_strip_this_month_s_consumption": {"state": "105.6"},
    }


def test_full_keeps_all_four_drives_and_dimms():
    """Regression: identical tags collapsed 4 readings into 1 on write."""
    out = compose_archbox_full(_full_rows(), _ha_states(), NOW)
    assert sorted(out["storage"]["nvme"]) == ["nvme0", "nvme1", "nvme2", "nvme3"]
    assert out["storage"]["nvme"]["nvme0"]["Composite"] == 41.9
    assert len(out["memory"]["dimm_temps"]) == 4
    assert out["memory"]["dimm_temps"]["9-0053"] == 37.5


def test_full_sections_present():
    out = compose_archbox_full(_full_rows(), _ha_states(), NOW)
    for section in ("cpu", "gpu_die", "gpu_memory", "gpu_vrm", "gpu_fans_rpm",
                    "loop", "board", "memory", "storage", "network", "power"):
        assert section in out, f"missing section {section}"
    assert out["cpu"]["ccd1"] == 30.1
    assert out["loop"]["coolant_temp"] == 26.1
    assert out["loop"]["chassis_fans_rpm"]["fan2"] == 0  # empty header, expected
    assert out["gpu_die"]["pstate"] == "P8"
    assert out["board"]["thermistor_14"] == 34.5


def test_full_derived_power_and_thermal():
    out = compose_archbox_full(_full_rows(), _ha_states(), NOW)
    assert out["power"]["non_gpu_w"] == 106.0     # 136.4 - 30.4
    assert out["gpu_die"]["hotspot_delta"] == 9.0  # 36.0 - 27.0
    assert out["loop"]["gpu_over_coolant"] == 0.9  # 27.0 - 26.1


def test_full_nvme_flag_uses_hottest_drive():
    rows = [r for r in _full_rows()
            if not (r["_measurement"] == "temp_detail" and r.get("chip") == "nvme")]
    rows.append(_row("temp_detail", "temp", 78.0, chip="nvme", dev="nvme0", label="Composite"))
    rows.append(_row("temp_detail", "temp", 40.0, chip="nvme", dev="nvme1", label="Composite"))
    out = compose_archbox_full(rows, _ha_states(), NOW)
    assert any(f["flag"] == "nvme_temp_critical" for f in out["flags"])


def test_full_disk_full_flag():
    rows = _full_rows() + [_row("disk", "used_percent", 94.0, path="/home")]
    out = compose_archbox_full(rows, _ha_states(), NOW)
    assert any(f["flag"] == "disk_full:/home" for f in out["flags"])


def test_full_empty_input_is_safe():
    out = compose_archbox_full([], {}, NOW)
    assert out["online"] is False
    assert out["cpu"]["tctl"] is None
