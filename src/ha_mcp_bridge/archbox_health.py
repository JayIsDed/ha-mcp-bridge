"""Archbox pulse — one-call health bundle for the workstation.

Mirrors the shelf_pulse pattern. The archbox is jay's main rig (Ryzen 9 7950X,
RTX 3090 FTW3 Ultra, X670E Taichi, custom water loop) and also the AI lab's
compute host: llama-swap's model shelf and the Unsloth Studio playground both
live on its GPU.

Data path: telegraf on the archbox -> InfluxDB (bucket "hosts") -> HA sensors
(influxdb flux platform) -> here. We read HA rather than Influx directly so
this needs no extra token and picks up PC-Strip wall power, which is HA-native
and is the only source for TOTAL system draw (PSU losses included).

See ~/git/ai-lab/docs/archbox-monitoring.md for what is and is not readable on
this hardware (the NCT6796D-S board chip is permanently dead under Linux).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .ha_client import HAClient
from .shelf_health import fetch_states

# --- registry -------------------------------------------------------------
# key -> (entity_id, kind). Adding a metric here surfaces it in vitals with no
# other code change, same contract as shelf_registry.
ARCHBOX_ENTITIES: dict[str, str] = {
    # thermals
    "cpu_temp": "sensor.archbox_cpu_temp",
    "gpu_temp": "sensor.archbox_gpu_temp",
    "gpu_hotspot": "sensor.archbox_gpu_hotspot",
    "gpu_vram": "sensor.archbox_gpu_vram_temp",
    "gpu_vrm": "sensor.archbox_gpu_vrm_temp",
    "coolant_temp": "sensor.archbox_coolant_temp",
    "nvme_temp": "sensor.archbox_nvme_temp",
    # load
    "cpu_load": "sensor.archbox_cpu_load",
    "gpu_load": "sensor.archbox_gpu_load",
    "memory_used": "sensor.archbox_memory_used",
    # power
    "gpu_power": "sensor.archbox_gpu_power",
    "wall_power": "sensor.pc_strip_current_consumption",
    "line_voltage": "sensor.pc_strip_voltage",
    "today_kwh": "sensor.pc_strip_today_s_consumption",
    "month_kwh": "sensor.pc_strip_this_month_s_consumption",
}

ARCHBOX_SWITCHES: dict[str, str] = {
    "power_switch": "switch.archbox",   # WoL on / authenticated poweroff
    "mains_switch": "switch.pc_strip",  # the strip feeding the whole rig
}

# thresholds -> (warn, critical). Chosen for this hardware specifically:
# 3090 throttles ~83C core; GDDR6X is rated far higher but 95C+ is where the
# memory junction gets unhappy; 7950X runs hot by design (95C is spec, not a
# fault) so the warn sits high; loop coolant climbing past ~35C means the
# radiators are losing.
THRESHOLDS: dict[str, tuple[float, float]] = {
    "cpu_temp": (85.0, 95.0),
    "gpu_temp": (80.0, 87.0),
    "gpu_hotspot": (95.0, 105.0),
    "gpu_vram": (95.0, 105.0),
    "gpu_vrm": (95.0, 110.0),
    "coolant_temp": (35.0, 45.0),
    "nvme_temp": (65.0, 75.0),
}


def _numeric(state: str | None) -> float | None:
    if state in (None, "unavailable", "unknown", "none", ""):
        return None
    try:
        return float(state)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def compose_archbox(states: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Pure function: raw HA states -> pulse payload. Unit-testable without HA."""
    vitals: dict[str, Any] = {}
    for key, entity_id in ARCHBOX_ENTITIES.items():
        raw = states.get(entity_id) or {}
        vitals[key] = _round(_numeric(raw.get("state")))

    for key, entity_id in ARCHBOX_SWITCHES.items():
        raw = states.get(entity_id) or {}
        vitals[key] = raw.get("state") or "unknown"

    # --- derived: the numbers you would otherwise compute by hand ---
    wall, gpu = vitals.get("wall_power"), vitals.get("gpu_power")
    vitals["non_gpu_power"] = _round(wall - gpu) if wall is not None and gpu is not None else None

    gpu_t, coolant = vitals.get("gpu_temp"), vitals.get("coolant_temp")
    # how hard the loop is working to hold the die down; grows under sustained load
    vitals["gpu_over_coolant"] = _round(gpu_t - coolant) if gpu_t is not None and coolant is not None else None

    hotspot = vitals.get("gpu_hotspot")
    # core-to-hotspot delta is the classic dried-out-paste / bad-mount tell
    vitals["hotspot_delta"] = _round(hotspot - gpu_t) if hotspot is not None and gpu_t is not None else None

    # --- flags ---
    flags: list[dict[str, Any]] = []
    online = vitals.get("power_switch") == "on"

    if not online:
        flags.append({
            "flag": "archbox_offline", "level": "info", "known": True,
            "message": "archbox is powered off; telemetry will be stale. "
                       "Wake with switch.archbox (magic packet).",
        })

    for key, (warn, crit) in THRESHOLDS.items():
        val = vitals.get(key)
        if val is None:
            if online:
                flags.append({
                    "flag": f"sensor_unavailable:{key}", "level": "warn", "known": False,
                    "message": f"{key} has no value while the box is on — check telegraf "
                               f"on the archbox, or the influxdb sensor platform in HA.",
                })
            continue
        if val >= crit:
            flags.append({"flag": f"{key}_critical", "level": "critical", "known": False,
                          "message": f"{key} = {val}C (critical >= {crit}C)"})
        elif val >= warn:
            flags.append({"flag": f"{key}_warn", "level": "warn", "known": False,
                          "message": f"{key} = {val}C (warn >= {warn}C)"})

    if online and vitals.get("mains_switch") == "off":
        flags.append({
            "flag": "mains_off_while_on", "level": "warn", "known": False,
            "message": "switch.pc_strip reads off but the archbox reads on — "
                       "one of the two is lying, check the strip.",
        })

    hd = vitals.get("hotspot_delta")
    if hd is not None and hd >= 25:
        flags.append({
            "flag": "hotspot_delta_high", "level": "warn", "known": False,
            "message": f"core-to-hotspot delta {hd}C — classic sign of degraded "
                       f"thermal paste or an uneven cooler mount on the 3090.",
        })

    summary = {
        "critical": sum(1 for f in flags if f["level"] == "critical"),
        "warn": sum(1 for f in flags if f["level"] == "warn"),
        "info": sum(1 for f in flags if f["level"] == "info"),
    }
    return {
        "timestamp": now.isoformat(),
        "online": online,
        "summary": summary,
        "vitals": vitals,
        "flags": flags,
    }


async def build_archbox_pulse(ha: HAClient) -> dict[str, Any]:
    """Fetch every archbox entity in parallel and compose the pulse."""
    entity_ids = list(ARCHBOX_ENTITIES.values()) + list(ARCHBOX_SWITCHES.values())
    states = await fetch_states(ha, entity_ids)
    return compose_archbox(states, datetime.now(timezone.utc))


# =========================================================================
# FULL SUITE — everything, sectioned. Reads InfluxDB directly rather than the
# 11 HA convenience sensors, so it reaches all 11 iCX3 channels, all 3 GPU
# fans, all 6 chassis fans, and every DIMM/drive as an ARRAY. Power still
# comes from HA, because the smart plug is the only source of wall draw.
# =========================================================================

# One query, last() per series. Deliberately selective: `cpu` alone has 33
# tag values x 11 fields, and `diskio` 17 x 14 — pulling everything would be
# thousands of series for a "glance" tool.
FULL_FLUX = """
from(bucket: "hosts")
  |> range(start: -5m)
  |> filter(fn: (r) =>
       r._measurement == "temp"
       or (r._measurement == "temp_detail" and (r.chip == "nvme" or r.chip == "spd5118"))
       or r._measurement == "icx3_temp"
       or r._measurement == "icx3_fan"
       or (r._measurement == "nvidia_smi" and (
             r._field == "temperature_gpu" or r._field == "power_draw"
             or r._field == "power_limit" or r._field == "utilization_gpu"
             or r._field == "utilization_memory" or r._field == "memory_used"
             or r._field == "memory_total" or r._field == "fan_speed"
             or r._field == "clocks_current_graphics" or r._field == "clocks_current_sm"
             or r._field == "clocks_current_memory"
             or r._field == "pcie_link_gen_current" or r._field == "pcie_link_width_current"))
       or (r._measurement == "prometheus" and r.device =~ /Commander/)
       or (r._measurement == "cpu" and r.cpu == "cpu-total" and r._field == "usage_active")
       or (r._measurement == "mem" and (r._field == "used_percent" or r._field == "used" or r._field == "total"))
       or (r._measurement == "swap" and r._field == "used_percent")
       or (r._measurement == "system" and (
             r._field == "load1" or r._field == "load5" or r._field == "load15"
             or r._field == "uptime" or r._field == "n_cpus"))
       or (r._measurement == "disk" and r._field == "used_percent")
       or (r._measurement == "processes" and (r._field == "total" or r._field == "running"))
  )
  |> last()
"""


def _num(row: dict[str, Any]) -> float | None:
    v = row.get("_value")
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def compose_archbox_full(
    rows: list[dict[str, Any]],
    states: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Pure function: influx rows + HA states -> sectioned snapshot."""
    temps: dict[str, list[float]] = {}
    icx: dict[str, float] = {}
    fans_gpu: dict[str, float] = {}
    nv: dict[str, float] = {}
    cc_temp: dict[str, float] = {}
    cc_fan: dict[str, float] = {}
    host: dict[str, float] = {}
    disks: dict[str, float] = {}
    # temp_detail carries a per-device tag, so these really are per-drive and
    # per-DIMM. inputs.temp cannot distinguish them — all 4 drives share the
    # single `sensor=nvme_composite` value, and all 4 DIMMs share `spd5118`.
    detail: dict[str, dict[str, dict[str, float]]] = {}
    pstate = None

    for r in rows:
        m, f, val = r.get("_measurement"), r.get("_field"), _num(r)
        if val is None:
            continue
        if m == "temp_detail":
            chip = str(r.get("chip"))
            detail.setdefault(chip, {}).setdefault(str(r.get("dev")), {})[
                str(r.get("label"))
            ] = _round(val)  # type: ignore[assignment]
        elif m == "temp":
            temps.setdefault(str(r.get("sensor")), []).append(val)
        elif m == "icx3_temp":
            icx[str(r.get("sensor"))] = val
        elif m == "icx3_fan":
            fans_gpu[f"fan{r.get('fan')}"] = val
        elif m == "nvidia_smi":
            nv[str(f)] = val
            pstate = r.get("pstate") or pstate
        elif m == "prometheus":
            if f == "coolercontrol_temperature_celsius":
                cc_temp[str(r.get("sensor"))] = val
            elif f == "coolercontrol_fan_rpm":
                cc_fan[str(r.get("channel"))] = val
        elif m == "disk":
            disks[str(r.get("path"))] = val
        else:
            host[f"{m}_{f}"] = val

    def t(name: str) -> float | None:
        vals = temps.get(name)
        return _round(vals[0]) if vals else None

    def tlist(name: str) -> list[float]:
        return sorted(_round(v) for v in temps.get(name, []))  # type: ignore[misc]

    def ha_num(entity: str) -> float | None:
        return _round(_numeric((states.get(entity) or {}).get("state")))

    wall = ha_num("sensor.pc_strip_current_consumption")
    gpu_w = _round(nv.get("power_draw"))
    gpu_t = _round(nv.get("temperature_gpu"))
    coolant = _round(cc_temp.get("temp0"))
    hotspot = _round(icx.get("hotspot"))

    snapshot: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "online": (states.get("switch.archbox") or {}).get("state") == "on",
        "cpu": {
            "tctl": t("k10temp_tctl"),
            "ccd1": t("k10temp_tccd1"),
            "ccd2": t("k10temp_tccd2"),
            "load_pct": _round(host.get("cpu_usage_active")),
            "load1": _round(host.get("system_load1"), 2),
            "load5": _round(host.get("system_load5"), 2),
            "load15": _round(host.get("system_load15"), 2),
            "threads": int(host["system_n_cpus"]) if "system_n_cpus" in host else None,
            "processes": int(host["processes_total"]) if "processes_total" in host else None,
            "running": int(host["processes_running"]) if "processes_running" in host else None,
        },
        "gpu_die": {
            "temp": gpu_t,
            "hotspot": hotspot,
            "gpu2": _round(icx.get("gpu2")),
            "hotspot_delta": _round(hotspot - gpu_t) if hotspot is not None and gpu_t is not None else None,
            "util_pct": _round(nv.get("utilization_gpu")),
            "mem_util_pct": _round(nv.get("utilization_memory")),
            "pstate": pstate,
            "clock_graphics_mhz": _round(nv.get("clocks_current_graphics"), 0),
            "clock_sm_mhz": _round(nv.get("clocks_current_sm"), 0),
            "clock_memory_mhz": _round(nv.get("clocks_current_memory"), 0),
            "pcie_gen": _round(nv.get("pcie_link_gen_current"), 0),
            "pcie_width": _round(nv.get("pcie_link_width_current"), 0),
        },
        # MEM1-3 are the trustworthy GDDR6X readings; vram/hotspot are flaky
        # per evga-icx's own author.
        "gpu_memory": {
            "mem1": _round(icx.get("mem1")), "mem2": _round(icx.get("mem2")),
            "mem3": _round(icx.get("mem3")),
            "vram_junction": _round(icx.get("vram")),
            "used_mib": _round(nv.get("memory_used"), 0),
            "total_mib": _round(nv.get("memory_total"), 0),
        },
        "gpu_vrm": {f"pwr{i}": _round(icx.get(f"pwr{i}")) for i in range(1, 6)},
        "gpu_fans_rpm": {k: _round(v, 0) for k, v in sorted(fans_gpu.items())},
        "gpu_fan_driver_pct": _round(nv.get("fan_speed")),
        # fan2 reads 0 — empty header. The 6th T30 is on a motherboard header
        # and is unreadable (NCT6796D-S is dead under Linux).
        "loop": {
            "coolant_temp": coolant,
            "chassis_fans_rpm": {k: _round(v, 0) for k, v in sorted(cc_fan.items())},
            "gpu_over_coolant": _round(gpu_t - coolant) if gpu_t is not None and coolant is not None else None,
        },
        "board": {k.replace("nct6686_", ""): t(k) for k in sorted(temps) if k.startswith("nct6686_")},
        "memory": {
            # per-module, keyed by SMBus address (9-0050 .. 9-0053)
            "dimm_temps": {
                dev: vals.get("temp1")
                for dev, vals in sorted(detail.get("spd5118", {}).items())
            } or {"_all": tlist("spd5118")},
            "used_pct": _round(host.get("mem_used_percent")),
            "used_gib": _round((host.get("mem_used") or 0) / 1024**3, 1) if "mem_used" in host else None,
            "total_gib": _round((host.get("mem_total") or 0) / 1024**3, 1) if "mem_total" in host else None,
            "swap_used_pct": _round(host.get("swap_used_percent")),
        },
        "storage": {
            # per-drive: {nvme0: {Composite, Sensor_1, Sensor_2}, ...}
            "nvme": {dev: vals for dev, vals in sorted(detail.get("nvme", {}).items())}
            or {"_all_composite": tlist("nvme_composite")},
            "filesystems_used_pct": {k: _round(v) for k, v in sorted(disks.items())},
        },
        "network": {"nic_temp": t("r8169_0_4d00:00"), "wifi_temp": t("iwlwifi_1")},
        "igpu_temp": t("amdgpu_edge"),
        "power": {
            "wall_w": wall,
            "gpu_w": gpu_w,
            "gpu_limit_w": _round(nv.get("power_limit"), 0),
            "non_gpu_w": _round(wall - gpu_w) if wall is not None and gpu_w is not None else None,
            "line_v": ha_num("sensor.pc_strip_voltage"),
            "today_kwh": ha_num("sensor.pc_strip_today_s_consumption"),
            "month_kwh": ha_num("sensor.pc_strip_this_month_s_consumption"),
            "mains_switch": (states.get("switch.pc_strip") or {}).get("state", "unknown"),
        },
        "uptime_hours": _round((host.get("system_uptime") or 0) / 3600, 1) if "system_uptime" in host else None,
    }

    # reuse the pulse's threshold evaluators so full and pulse never disagree
    pulse_view = {
        "cpu_temp": snapshot["cpu"]["tctl"], "gpu_temp": gpu_t,
        "gpu_hotspot": hotspot, "gpu_vram": snapshot["gpu_memory"]["vram_junction"],
        "gpu_vrm": max([v for v in snapshot["gpu_vrm"].values() if v is not None], default=None),
        "coolant_temp": coolant,
        "nvme_temp": max(
            [
                v.get("Composite")
                for v in snapshot["storage"]["nvme"].values()
                if isinstance(v, dict) and v.get("Composite") is not None
            ],
            default=None,
        ),
    }
    flags: list[dict[str, Any]] = []
    if not snapshot["online"]:
        flags.append({"flag": "archbox_offline", "level": "info", "known": True,
                      "message": "archbox is powered off; telemetry is stale."})
    for key, (warn, crit) in THRESHOLDS.items():
        val = pulse_view.get(key)
        if val is None:
            continue
        if val >= crit:
            flags.append({"flag": f"{key}_critical", "level": "critical", "known": False,
                          "message": f"{key} = {val}C (critical >= {crit}C)"})
        elif val >= warn:
            flags.append({"flag": f"{key}_warn", "level": "warn", "known": False,
                          "message": f"{key} = {val}C (warn >= {warn}C)"})
    hd = snapshot["gpu_die"]["hotspot_delta"]
    if hd is not None and hd >= 25:
        flags.append({"flag": "hotspot_delta_high", "level": "warn", "known": False,
                      "message": f"core-to-hotspot delta {hd}C — degraded paste or bad mount."})
    for path, pct in snapshot["storage"]["filesystems_used_pct"].items():
        if pct is not None and pct >= 90:
            flags.append({"flag": f"disk_full:{path}", "level": "warn", "known": False,
                          "message": f"{path} is {pct}% full"})

    snapshot["summary"] = {
        "critical": sum(1 for f in flags if f["level"] == "critical"),
        "warn": sum(1 for f in flags if f["level"] == "warn"),
        "info": sum(1 for f in flags if f["level"] == "info"),
    }
    snapshot["flags"] = flags
    return snapshot


async def build_archbox_full(ha: HAClient, influx: Any) -> dict[str, Any]:
    """Query InfluxDB for the whole sensor suite + HA for power, then compose."""
    rows = await influx.query(FULL_FLUX)
    states = await fetch_states(
        ha,
        [
            "switch.archbox", "switch.pc_strip",
            "sensor.pc_strip_current_consumption", "sensor.pc_strip_voltage",
            "sensor.pc_strip_today_s_consumption",
            "sensor.pc_strip_this_month_s_consumption",
        ],
    )
    return compose_archbox_full(rows, states, datetime.now(timezone.utc))
