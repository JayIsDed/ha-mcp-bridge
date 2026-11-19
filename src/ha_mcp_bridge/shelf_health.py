"""Shelf health orchestrator — composes registry + live states + flags into a
compact snapshot response for MCP tools.

This module is pure orchestration. It owns:
- Parallel state fetching via HAClient
- Organizing responses by category for readable output
- Running flag evaluators against the snapshot
- Trimming / rounding numeric values so responses stay small

It owns NO side effects on HA (read-only) and NO policy decisions (all thresholds
live in the registry or the flag module).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .ha_client import HAClient, HAError
from .shelf_flags import evaluate_all
from .shelf_registry import SHELF_ENTITIES, active_entity_ids


# ─────────────────────────────────────────────────────────────────────────────
# Fetching
# ─────────────────────────────────────────────────────────────────────────────


class States(dict):
    """Entity states keyed by entity_id, plus `read_errors`: the entity_ids
    whose read failed (after one retry), each with its error."""

    read_errors: dict[str, str]

    def __init__(self, *args: Any, read_errors: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read_errors = read_errors or {}


async def fetch_states(
    ha: HAClient,
    entity_ids: list[str],
) -> States:
    """Fetch states for a list of entity_ids in parallel. Returns a dict keyed
    by entity_id.

    An entity HA doesn't know (404) is left out of the dict; the flag
    evaluators treat a missing entity as dark, so it is reported, not dropped.
    A read that fails while others succeed is retried once, since one timeout
    among a few dozen parallel reads is usually a hiccup; if it fails again it
    is left out too and listed in `read_errors`, so the flag can say the read
    failed rather than that the entity is gone.

    Raises HAError when no read succeeded at all: HA is down, hung, or rejected
    the token. Swallowing that case left every caller with an empty snapshot,
    which the evaluators read as "nothing wrong".
    """

    async def one(eid: str) -> tuple[str, dict[str, Any] | None, HAError | None]:
        try:
            raw = await ha.get_state(eid)
        except HAError as e:
            return eid, None, e
        return eid, raw, None

    results = await asyncio.gather(*(one(eid) for eid in entity_ids))
    failed = [eid for eid, _, err in results if err is not None]
    if entity_ids and len(failed) == len(entity_ids):
        first = next(err for _, _, err in results if err is not None)
        raise HAError(
            f"Could not read any of the {len(entity_ids)} entities from HA, so there "
            f"is no health to report. First error: {first}"
        )
    if failed:
        retried = await asyncio.gather(*(one(eid) for eid in failed))
        results = [r for r in results if r[2] is None] + list(retried)
    return States(
        {eid: raw for eid, raw, _ in results if raw is not None},
        read_errors={eid: str(err) for eid, _, err in results if err is not None},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-entity formatting
# ─────────────────────────────────────────────────────────────────────────────


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _numeric(state: str | None) -> float | None:
    if state in (None, "unavailable", "unknown", "none", ""):
        return None
    try:
        return float(state)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _compact_sensor(
    key: str,
    entry: dict[str, Any],
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compact format for a thermal/chemistry/power sensor."""
    if raw is None:
        return {
            "status": "not_fetched",
            "entity_id": entry["entity_id"],
        }

    state = raw.get("state")
    numeric = _numeric(state)

    item: dict[str, Any] = {}
    if numeric is not None:
        item["value"] = _round(numeric)
    else:
        item["state"] = state
        item["status"] = "unavailable" if state in ("unavailable", "unknown") else "ok"

    if entry.get("unit"):
        item["unit"] = entry["unit"]
    if raw.get("last_changed"):
        item["last_changed"] = raw["last_changed"]
    if entry.get("target") is not None and numeric is not None:
        item["target"] = entry["target"]
        item["delta"] = _round(numeric - entry["target"])
    if entry.get("range") and numeric is not None:
        lo, hi = entry["range"]
        if lo <= numeric <= hi:
            item["range_status"] = "in"
        else:
            item["range_status"] = "above" if numeric > hi else "below"
    if entry.get("known_state"):
        item["note"] = entry["known_state"]
    return item


def _compact_climate(
    key: str,
    entry: dict[str, Any],
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    if raw is None:
        return {"status": "not_fetched", "entity_id": entry["entity_id"]}
    attrs = raw.get("attributes") or {}
    item: dict[str, Any] = {
        "mode": raw.get("state"),
        "hvac_action": attrs.get("hvac_action"),
        "target": attrs.get("temperature"),
        "current": attrs.get("current_temperature"),
        "last_changed": raw.get("last_changed"),
    }
    if entry.get("known_state"):
        item["note"] = entry["known_state"]
    return item


def _compact_switch(raw: dict[str, Any] | None) -> str:
    """Just 'on'/'off'/'unavailable' — switches don't need full metadata."""
    if raw is None:
        return "not_fetched"
    return str(raw.get("state", "unknown"))


def _compact_light(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {"status": "not_fetched"}
    attrs = raw.get("attributes") or {}
    brightness = attrs.get("brightness")
    item: dict[str, Any] = {"state": raw.get("state")}
    if brightness is not None:
        item["brightness"] = brightness
        item["brightness_pct"] = round(brightness * 100 / 255)
    item["last_changed"] = raw.get("last_changed")
    return item


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot composition
# ─────────────────────────────────────────────────────────────────────────────


def compose_snapshot(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose the final response from pre-fetched states. Pure function — no I/O."""
    if registry is None:
        registry = SHELF_ENTITIES
    if now is None:
        now = datetime.now(timezone.utc)

    # Group active entities by category for readable output.
    thermal: dict[str, dict[str, Any]] = {}
    chemistry: dict[str, dict[str, Any]] = {}
    power: dict[str, dict[str, Any]] = {}
    climate_entries: dict[str, dict[str, Any]] = {}
    light_entries: dict[str, dict[str, Any]] = {}
    camera_entries: dict[str, dict[str, Any]] = {}
    weather: dict[str, dict[str, Any]] = {}
    infra: dict[str, dict[str, Any]] = {}

    # Counts for the summary block.
    n_active = 0
    n_offline = 0

    for key, entry in registry.items():
        if not entry.get("active"):
            continue
        n_active += 1
        raw = states.get(entry["entity_id"])
        is_offline = raw is None or raw.get("state") in ("unavailable", "unknown")
        if is_offline:
            n_offline += 1

        category = entry.get("category", "infrastructure")
        role = entry.get("role")

        if category == "thermal":
            thermal[key] = _compact_sensor(key, entry, raw)
        elif category == "chemistry":
            chemistry[key] = _compact_sensor(key, entry, raw)
        elif category == "climate":
            climate_entries[key] = _compact_climate(key, entry, raw)
        elif category == "light":
            if role == "grow_light":
                light_entries[key] = _compact_light(raw)
            else:
                light_entries[key] = _compact_sensor(key, entry, raw)
        elif category == "power":
            if role == "switch":
                power[key] = _compact_switch(raw)
            else:
                power[key] = _compact_sensor(key, entry, raw)
        elif category == "weather":
            weather[key] = _compact_sensor(key, entry, raw)
        elif category == "camera":
            camera_entries[key] = _compact_sensor(key, entry, raw)
        elif category == "infrastructure":
            infra[key] = _compact_sensor(key, entry, raw)

    # Flags.
    flags = evaluate_all(states, registry, now)

    flag_counts = {"critical": 0, "warn": 0, "info": 0}
    for f in flags:
        lvl = f.get("level", "info")
        if lvl in flag_counts:
            flag_counts[lvl] += 1

    snapshot: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "summary": {
            "active_entities": n_active,
            "offline": n_offline,
            "flags": flag_counts,
        },
        "flags": flags,
    }

    # Only include sections that have content — keeps response small.
    if thermal:
        snapshot["thermal"] = thermal
    if chemistry:
        snapshot["chemistry"] = chemistry
    if power:
        snapshot["power"] = power
    if climate_entries:
        snapshot["climate"] = climate_entries
    if light_entries:
        snapshot["light"] = light_entries
    if camera_entries:
        snapshot["camera"] = camera_entries
    if weather:
        snapshot["weather"] = weather
    if infra:
        snapshot["infrastructure"] = infra

    return snapshot


async def build_snapshot(ha: HAClient) -> dict[str, Any]:
    """Full pipeline: fetch active-entity states, compose, return."""
    eids = active_entity_ids()
    states = await fetch_states(ha, eids)
    return compose_snapshot(states)


def compose_vitals(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Tight "at-a-glance" vitals block — the numbers you want whether or not
    flags are firing. Always populated even on a fully healthy shelf.

    Pure function — no I/O.
    """
    if registry is None:
        registry = SHELF_ENTITIES

    def _val(key: str) -> float | None:
        entry = registry.get(key)
        if not entry:
            return None
        raw = states.get(entry["entity_id"])
        if not raw:
            return None
        s = raw.get("state")
        if s in (None, "unavailable", "unknown", "none", ""):
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    def _round(v: float | None, digits: int = 2) -> float | None:
        return round(v, digits) if v is not None else None

    # Tank thermal vitals
    tank_center = _val("tank_center")
    tank_target = registry.get("tank_center", {}).get("target")

    # Pull live climate target if available (follows runtime setpoint changes).
    climate_raw = states.get("climate.main_tank")
    if climate_raw:
        attrs = climate_raw.get("attributes") or {}
        try:
            tank_target = float(attrs.get("temperature"))
        except (TypeError, ValueError):
            pass
    heater_calling = False
    if climate_raw:
        heater_calling = (climate_raw.get("attributes") or {}).get("hvac_action") == "heating"

    tank_delta = None
    if tank_center is not None and tank_target is not None:
        tank_delta = tank_center - tank_target

    # Chemistry
    tds_tank = _val("tds_tank")
    tds_status: str | None = None
    if tds_tank is not None:
        rng = registry.get("tds_tank", {}).get("range")
        if rng:
            lo, hi = rng
            if lo <= tds_tank <= hi:
                tds_status = "in"
            elif tds_tank > hi:
                tds_status = "above"
            else:
                tds_status = "below"

    vitals: dict[str, Any] = {
        # Tank thermal
        "tank_center": _round(tank_center),
        "tank_target": tank_target,
        "tank_delta": _round(tank_delta),
        "heater_power": _round(_val("heater_power"), 1),
        "heater_calling": heater_calling,
        "tank_substrate": _round(_val("tank_substrate")),
        # Chemistry
        "tds_tank": _round(tds_tank, 0) if tds_tank is not None else None,
        "tds_status": tds_status,
        # Ambient
        "shelf_ambient": _round(_val("shelf_ambient"), 1),
        "outside_temp": _round(_val("outside_temp"), 0),
        "forecast_5d_min_low": _round(_val("forecast_5d_min_low"), 0),
        # Power envelope
        "l0_power": _round(_val("l0_power"), 1),
        # Derived
        "basement_delta": (
            _round(_val("shelf_ambient") - _val("outside_temp"), 1)
            if _val("shelf_ambient") is not None and _val("outside_temp") is not None
            else None
        ),
    }
    return vitals


async def build_pulse(ha: HAClient) -> dict[str, Any]:
    """Quick pulse — vitals + flags in one small payload.

    Use this as the mid-day "anything wrong + what are the key numbers" check.
    Returns a compact block of the sensors you care about most (tank temp,
    heater power, TDS, ambient, weather), plus the anomaly flag list.
    """
    eids = active_entity_ids()
    states = await fetch_states(ha, eids)
    now = datetime.now(timezone.utc)

    flags = evaluate_all(states, SHELF_ENTITIES, now)
    flag_counts = {"critical": 0, "warn": 0, "info": 0}
    for f in flags:
        lvl = f.get("level", "info")
        if lvl in flag_counts:
            flag_counts[lvl] += 1

    return {
        "timestamp": now.isoformat(),
        "summary": flag_counts,
        "vitals": compose_vitals(states, SHELF_ENTITIES),
        "flags": flags,
    }
