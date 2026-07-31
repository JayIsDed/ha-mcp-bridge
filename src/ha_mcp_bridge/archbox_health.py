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
