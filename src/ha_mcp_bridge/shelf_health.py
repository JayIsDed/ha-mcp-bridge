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


async def fetch_states(
    ha: HAClient,
    entity_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch states for a list of entity_ids in parallel. Returns a dict keyed
    by entity_id. Entities that error or return None are omitted.
    """

    async def one(eid: str) -> tuple[str, dict[str, Any] | None]:
        try:
            raw = await ha.get_state(eid)
        except HAError:
            return eid, None
        return eid, raw

    results = await asyncio.gather(*(one(eid) for eid in entity_ids))
    return {eid: raw for eid, raw in results if raw is not None}


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


async def build_flags_only(ha: HAClient) -> dict[str, Any]:
    """Just the flag output — cheapest "anything wrong?" check."""
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
        "flags": flags,
    }
