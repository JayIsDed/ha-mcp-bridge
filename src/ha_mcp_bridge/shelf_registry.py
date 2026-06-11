"""Shelf entity registry — single source of truth for calibration-shelf MCP tools.

This module encodes which entities the `shelf_*` tools operate on, and the metadata
each entity carries (category, container, bounds, activation state). Flags and the
orchestrator both read from this registry so that:

1. Adding a new physical sensor = one entry here, no tool-code changes elsewhere.
2. Gating a sensor OFF (e.g. bucket TDS probe not plugged in) = `active: False`, no
   tool-code changes elsewhere.
3. The dashboard-side `entities.js` and this registry stay coherent via code review,
   not via duplicated string literals.

Entry schema (see `EntitySpec` below for the typed version):
    entity_id: str          fully-qualified HA entity id
    category:  str          thermal | chemistry | power | climate | light | camera |
                            weather | infrastructure
    container: str          physical grouping: main_tank_10g | bucket_rig | caridina_5g |
                            canopy | shelf_ambient | shelf_outlets | external | infrastructure
    role:      str          what this measures/controls (water_temp, tds, heater_power,
                            hvac_action, outlet_power, illuminance, ...)
    unit:      str | None   display unit (°F, ppm, W, %, lx, ...)
    active:    bool         is this deployed + reporting right now? False = registered
                            but skipped by tools.

    Optional bounds (present only where flags need them):
    target:    float        setpoint (for thermal control)
    band:      float        ± tolerance band around target (hysteresis)
    flag_band: float        ± band used for "breach" flags (usually wider than `band`)
    range:     (lo, hi)     acceptable range (for chemistry metrics)

    Optional hints:
    known_state: str        why this entity may be in an odd state ("decommissioned",
                            "reading air not water", "known-offline")
    gated_by:    str         entity_id of a switch that enables/disables this sensor
    powered_by:  str         entity_id of the outlet powering the device this sensor
                             observes — used by phantom-heat style flags
    stale_dark_lux: float    illuminance sensors only — if the sensor goes stale (stops
                             reporting) while its value is below this lux threshold, the
                             staleness is expected (a delta-reporting lux sensor idles at
                             its dark floor) and is flagged info/known instead of warn.
                             A stale read ABOVE the threshold is a real stall → warn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EntitySpec:
    """Typed view of a registry entry. Registry stores plain dicts for easy extension;
    convert via `spec()` when you want type-checked access.
    """

    key: str
    entity_id: str
    category: str
    container: str
    role: str
    unit: str | None = None
    active: bool = True
    target: float | None = None
    band: float | None = None
    flag_band: float | None = None
    range: tuple[float, float] | None = None
    known_state: str | None = None
    gated_by: str | None = None
    powered_by: str | None = None
    stale_dark_lux: float | None = None
    notes: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Registry — shelf entities organized by logical key
# ─────────────────────────────────────────────────────────────────────────────

SHELF_ENTITIES: dict[str, dict[str, Any]] = {
    # ═══ Thermal (DS18B20 array on plant_shelf_temperatures board) ═══════════
    "tank_center": {
        "entity_id": "sensor.plant_shelf_temperatures_tank_center",
        "category": "thermal",
        "container": "main_tank_10g",
        "role": "water_temp",
        "unit": "°F",
        "active": True,
        "target": 77.0,
        "band": 0.5,
        "flag_band": 1.0,
        "notes": "Primary L1 control input, cross-cal reference for L2 Inkbird.",
    },
    "tank_substrate": {
        "entity_id": "sensor.plant_shelf_temperatures_tank_substrate",
        "category": "thermal",
        "container": "main_tank_10g",
        "role": "water_temp",
        "unit": "°F",
        "active": True,
        "target": 77.0,
        "flag_band": 1.5,
        "notes": "Near-substrate probe; pairs with tank_center for stratification check.",
    },
    "shelf_ambient": {
        "entity_id": "sensor.plant_shelf_temperatures_shelf_ambient",
        "category": "thermal",
        "container": "shelf_ambient",
        "role": "air_temp",
        "unit": "°F",
        "active": True,
        "range": (60.0, 75.0),
        "notes": "Basement air at shelf level; insulation + heater-duty correlation input.",
    },
    "bucket_rig_water": {
        "entity_id": "sensor.plant_shelf_temperatures_bucket_rig_water",
        "category": "thermal",
        "container": "bucket_rig",
        "role": "water_temp",
        "unit": "°F",
        "active": True,
        "known_state": "Bucket decommissioned 2026-04-22; probe now air-exposed.",
    },
    "probe_5_spare": {
        "entity_id": "sensor.plant_shelf_temperatures_probe_5_spare",
        "category": "thermal",
        "container": "shelf_ambient",
        "role": "auxiliary_temp",
        "unit": "°F",
        "active": True,
        "known_state": "Repurposed to LED PSU case temp (temporary).",
    },

    # ═══ Canopy (SHT3x + BH1750 on plant_shelf_canopy board) ═════════════════
    "canopy_temp": {
        "entity_id": "sensor.plant_shelf_canopy_canopy_temperature",
        "category": "thermal",
        "container": "canopy",
        "role": "air_temp",
        "unit": "°F",
        "active": True,
    },
    "canopy_humidity": {
        "entity_id": "sensor.plant_shelf_canopy_canopy_humidity",
        "category": "thermal",
        "container": "canopy",
        "role": "humidity",
        "unit": "%",
        "active": True,
    },
    "canopy_illuminance": {
        "entity_id": "sensor.plant_shelf_canopy_canopy_illuminance",
        "category": "light",
        "container": "canopy",
        "role": "illuminance",
        "unit": "lx",
        "active": True,
        "stale_dark_lux": 10.0,
        "notes": (
            "BH1750 delta-reports; idles at its dark floor (observed 0.0lx) overnight "
            "with grow + room lights off, which trips sensor_stale. stale_dark_lux gates "
            "that to info/known (residual equipment glow ~4.8lx stays noisy enough to "
            "self-clear; lit readings are hundreds+). A stale read above 10lx is a real "
            "stall → warn. Confirmed 2026-06-11 via deliberate basement-light ping."
        ),
    },

    # ═══ Water chemistry (tank_chemistry board, XIAO ESP32-C6 + ADS1115) ═════
    "tds_tank": {
        "entity_id": "sensor.tank_chemistry_tds_tank",
        "category": "chemistry",
        "container": "main_tank_10g",
        "role": "tds",
        "unit": "ppm",
        "active": True,
        "range": (200.0, 350.0),  # neo ideal
        "gated_by": "switch.tank_chemistry_tds_tank_enabled",
        "notes": "Temp-compensated against tank_center via firmware import.",
    },
    "tds_bucket": {
        "entity_id": "sensor.tank_chemistry_tds_bucket",
        "category": "chemistry",
        "container": "bucket_rig",
        "role": "tds",
        "unit": "ppm",
        "active": False,  # probe not physically plugged in
        "gated_by": "switch.tank_chemistry_tds_bucket_enabled",
    },
    "tds_caridina": {
        "entity_id": "sensor.tank_chemistry_tds_caridina",
        "category": "chemistry",
        "container": "caridina_5g",
        "role": "tds",
        "unit": "ppm",
        "active": False,  # Phase 2 reserved
        "range": (120.0, 180.0),  # caridina ideal
        "gated_by": "switch.tank_chemistry_tds_caridina_enabled",
    },
    # pH probe — hardware pending, firmware placeholder in tank-chem-board.yaml.
    # Flip `active` to True after physical install + firmware reflash.
    "ph_tank": {
        "entity_id": "sensor.tank_chemistry_ph_tank",
        "category": "chemistry",
        "container": "main_tank_10g",
        "role": "ph",
        "unit": "pH",
        "active": False,
        "range": (6.5, 7.8),
    },

    # ═══ Soil moisture (canopy-additions.yaml — hardware pending) ════════════
    # Once canopy board returns + ADS1115 expansion lands, flip active → True.
    "soil_mimosa_a": {
        "entity_id": "sensor.plant_shelf_canopy_soil_mimosa_a",
        "category": "chemistry",
        "container": "canopy",
        "role": "soil_moisture",
        "unit": "%",
        "active": False,
        "range": (25.0, 80.0),
    },
    "soil_mimosa_b": {
        "entity_id": "sensor.plant_shelf_canopy_soil_mimosa_b",
        "category": "chemistry",
        "container": "canopy",
        "role": "soil_moisture",
        "unit": "%",
        "active": False,
        "range": (25.0, 80.0),
    },
    "soil_basil": {
        "entity_id": "sensor.plant_shelf_canopy_soil_basil",
        "category": "chemistry",
        "container": "canopy",
        "role": "soil_moisture",
        "unit": "%",
        "active": False,
        "range": (30.0, 70.0),
    },

    # ═══ Climate helpers ══════════════════════════════════════════════════════
    "climate_main_tank": {
        "entity_id": "climate.main_tank",
        "category": "climate",
        "container": "main_tank_10g",
        "role": "thermostat",
        "unit": None,
        "active": True,
        "target": 77.0,
        "powered_by": "switch.cal_shelf_inkbird_10g",
    },
    "climate_bucket_rig": {
        "entity_id": "climate.bucket_rig",
        "category": "climate",
        "container": "bucket_rig",
        "role": "thermostat",
        "unit": None,
        "active": True,
        "known_state": "Bucket decommissioned 2026-04-22; expect hvac_mode=off.",
        "powered_by": "switch.calibration_shelf_strip_tapo_p316m_2",
    },

    # ═══ Power — Kasa heater plug (Layer 1 primary control) ═════════════════
    "heater_power": {
        "entity_id": "sensor.cal_shelf_inkbird_10g_current_consumption",
        "category": "power",
        "container": "main_tank_10g",
        "role": "heater_power",
        "unit": "W",
        "active": True,
        "range": (0.0, 150.0),  # normal firing window; >150 flags overdraw
    },
    "heater_switch": {
        "entity_id": "switch.cal_shelf_inkbird_10g",
        "category": "power",
        "container": "main_tank_10g",
        "role": "switch",
        "unit": None,
        "active": True,
    },
    "heater_voltage": {
        "entity_id": "sensor.cal_shelf_inkbird_10g_voltage",
        "category": "power",
        "container": "main_tank_10g",
        "role": "voltage",
        "unit": "V",
        "active": True,
        "range": (115.0, 125.0),
    },

    # ═══ Power — Tapo P316M strip outlets ═══════════════════════════════════
    "outlet_1_pump_power": {
        "entity_id": "sensor.calibration_shelf_strip_tapo_p316m_1_current_consumption",
        "category": "power",
        "container": "main_tank_10g",
        "role": "outlet_power",
        "unit": "W",
        "active": True,
        "notes": "Main tank pump — household convention: always outlet 1.",
    },
    "outlet_1_pump_switch": {
        "entity_id": "switch.calibration_shelf_strip_tapo_p316m_1",
        "category": "power",
        "container": "main_tank_10g",
        "role": "switch",
        "unit": None,
        "active": True,
    },
    "outlet_2_bucket_power": {
        "entity_id": "sensor.calibration_shelf_strip_tapo_p316m_2_current_consumption",
        "category": "power",
        "container": "bucket_rig",
        "role": "outlet_power",
        "unit": "W",
        "active": True,
    },
    "outlet_2_bucket_switch": {
        "entity_id": "switch.calibration_shelf_strip_tapo_p316m_2",
        "category": "power",
        "container": "bucket_rig",
        "role": "switch",
        "unit": None,
        "active": True,
    },
    "outlet_3_led_power": {
        "entity_id": "sensor.calibration_shelf_strip_tapo_p316m_3_current_consumption",
        "category": "power",
        "container": "shelf_outlets",
        "role": "outlet_power",
        "unit": "W",
        "active": True,
        "notes": "LED PSU (Mean Well 12V/20A → COB + future RGB).",
    },
    "outlet_3_led_switch": {
        "entity_id": "switch.calibration_shelf_strip_tapo_p316m_3",
        "category": "power",
        "container": "shelf_outlets",
        "role": "switch",
        "unit": None,
        "active": True,
    },
    "outlet_4_power": {
        "entity_id": "sensor.calibration_shelf_strip_tapo_p316m_4_current_consumption",
        "category": "power",
        "container": "shelf_outlets",
        "role": "outlet_power",
        "unit": "W",
        "active": True,
        "notes": "Unallocated.",
    },
    "outlet_5_power": {
        "entity_id": "sensor.calibration_shelf_strip_tapo_p316m_5_current_consumption",
        "category": "power",
        "container": "shelf_outlets",
        "role": "outlet_power",
        "unit": "W",
        "active": True,
        "notes": "Unallocated.",
    },
    "outlet_6_power": {
        "entity_id": "sensor.calibration_shelf_strip_tapo_p316m_6_current_consumption",
        "category": "power",
        "container": "shelf_outlets",
        "role": "outlet_power",
        "unit": "W",
        "active": True,
        "notes": "Unallocated (was reserved for DOMMIA, not yet plugged).",
    },

    # ═══ L0 Nuclear — 3D-Printer Strip (master shelf cutoff) ═════════════════
    "l0_power": {
        "entity_id": "sensor.3d_printer_strip_current_consumption",
        "category": "power",
        "container": "infrastructure",
        "role": "master_power",
        "unit": "W",
        "active": True,
        "notes": "Upstream of everything; includes 3D printers + shelf combined.",
    },
    "l0_switch": {
        "entity_id": "switch.3d_printer_strip",
        "category": "power",
        "container": "infrastructure",
        "role": "switch",
        "unit": None,
        "active": True,
    },

    # ═══ Grow light ═══════════════════════════════════════════════════════════
    "grow_white": {
        "entity_id": "light.grow_white",
        "category": "light",
        "container": "canopy",
        "role": "grow_light",
        "unit": None,
        "active": True,
    },
    "grow_light_wifi": {
        "entity_id": "sensor.plant_shelf_lights_wifi_signal",
        "category": "infrastructure",
        "container": "canopy",
        "role": "wifi_signal",
        "unit": "dBm",
        "active": True,
        "range": (-80.0, -30.0),
    },
    "grow_light_uptime": {
        "entity_id": "sensor.plant_shelf_lights_uptime",
        "category": "infrastructure",
        "container": "canopy",
        "role": "uptime",
        "unit": "s",
        "active": True,
    },

    # ═══ Camera (Reolink E1 Zoom) ════════════════════════════════════════════
    "camera_day_night": {
        "entity_id": "sensor.calibration_shelf_day_night_state",
        "category": "camera",
        "container": "infrastructure",
        "role": "ir_mode",
        "unit": None,
        "active": True,
    },
    "camera_pan": {
        "entity_id": "sensor.calibration_shelf_ptz_pan_position",
        "category": "camera",
        "container": "infrastructure",
        "role": "ptz_position",
        "unit": None,
        "active": True,
    },
    "camera_tilt": {
        "entity_id": "sensor.calibration_shelf_ptz_tilt_position",
        "category": "camera",
        "container": "infrastructure",
        "role": "ptz_position",
        "unit": None,
        "active": True,
    },

    # ═══ External — weather (met.no via HA weather.forecast_home) ════════════
    "outside_temp": {
        "entity_id": "sensor.outside_temperature",
        "category": "weather",
        "container": "external",
        "role": "air_temp",
        "unit": "°F",
        "active": True,
    },
    "outside_humidity": {
        "entity_id": "sensor.outside_humidity",
        "category": "weather",
        "container": "external",
        "role": "humidity",
        "unit": "%",
        "active": True,
    },
    "outside_pressure": {
        "entity_id": "sensor.outside_pressure",
        "category": "weather",
        "container": "external",
        "role": "pressure",
        "unit": "inHg",
        "active": True,
    },
    "outside_dew_point": {
        "entity_id": "sensor.outside_dew_point",
        "category": "weather",
        "container": "external",
        "role": "dew_point",
        "unit": "°F",
        "active": True,
    },
    "forecast_low_tomorrow": {
        "entity_id": "sensor.outside_low_tomorrow",
        "category": "weather",
        "container": "external",
        "role": "forecast_low",
        "unit": "°F",
        "active": True,
    },
    "forecast_5d_min_low": {
        "entity_id": "sensor.outside_5_day_min_low",
        "category": "weather",
        "container": "external",
        "role": "forecast_min",
        "unit": "°F",
        "active": True,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────────────────────


def spec(key: str) -> EntitySpec:
    """Return a typed EntitySpec for a registry key, or raise KeyError."""
    raw = SHELF_ENTITIES[key]
    return EntitySpec(key=key, **{k: v for k, v in raw.items() if k != "notes"}, notes=raw.get("notes"))


def active_keys() -> list[str]:
    """Keys of currently-deployed entities (active=True)."""
    return [k for k, v in SHELF_ENTITIES.items() if v.get("active")]


def active_entity_ids() -> list[str]:
    """HA entity IDs of currently-deployed entities."""
    return [v["entity_id"] for v in SHELF_ENTITIES.values() if v.get("active")]


def all_entity_ids() -> list[str]:
    """HA entity IDs of everything in the registry (active or not).

    Useful for broad diagnostic pulls when you explicitly want to see that an
    expected sensor is still unavailable.
    """
    return [v["entity_id"] for v in SHELF_ENTITIES.values()]


def by_category(category: str, active_only: bool = True) -> dict[str, dict[str, Any]]:
    """All registry entries in a given category. Active-only by default."""
    return {
        k: v
        for k, v in SHELF_ENTITIES.items()
        if v.get("category") == category and (not active_only or v.get("active"))
    }


def by_container(container: str, active_only: bool = True) -> dict[str, dict[str, Any]]:
    """All registry entries for a physical container (main_tank_10g, canopy, etc.)."""
    return {
        k: v
        for k, v in SHELF_ENTITIES.items()
        if v.get("container") == container and (not active_only or v.get("active"))
    }


def by_role(role: str, active_only: bool = True) -> dict[str, dict[str, Any]]:
    """All registry entries for a given role (water_temp, tds, outlet_power, ...)."""
    return {
        k: v
        for k, v in SHELF_ENTITIES.items()
        if v.get("role") == role and (not active_only or v.get("active"))
    }


def find_key(entity_id: str) -> str | None:
    """Reverse lookup — registry key for a given HA entity id, or None."""
    for k, v in SHELF_ENTITIES.items():
        if v.get("entity_id") == entity_id:
            return k
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Category / container taxonomies — helps consumers enumerate without magic
# strings. Keep in sync with the registry above.
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES = (
    "thermal",
    "chemistry",
    "power",
    "climate",
    "light",
    "camera",
    "weather",
    "infrastructure",
)

CONTAINERS = (
    "main_tank_10g",
    "bucket_rig",
    "caridina_5g",
    "canopy",
    "shelf_ambient",
    "shelf_outlets",
    "external",
    "infrastructure",
)
