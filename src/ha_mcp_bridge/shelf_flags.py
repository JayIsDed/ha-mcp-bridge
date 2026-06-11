"""Anomaly flag evaluators for the shelf MCP tools.

Each flag is a pure function: takes a snapshot of entity states + the registry +
the current time, and returns either None (no flag) or a flag dict with:

    {
        "flag":  str,               # unique short name
        "level": "info" | "warn" | "critical",
        "since": str | None,        # ISO timestamp the condition started (if known)
        "message": str,             # human-readable summary
        "known":   bool,            # True = user-acknowledged expected state
    }

The orchestrator iterates every function in `FLAG_EVALUATORS` and collects the
non-None results. Flags are deterministic on the inputs — no hidden I/O, no
dependence on external state beyond what's passed in. That makes them trivially
testable and safe to compose.

Level semantics:
- info:     informational, no action required (known-offline board, weather note)
- warn:     something to address but not urgent (phantom-heat config, mild drift)
- critical: immediate organism risk (tank way outside band, heater overdraw,
            critical sensor stale)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _state_value(raw: dict[str, Any] | None) -> float | None:
    """Coerce an HA state dict's 'state' field to float. Returns None if
    unavailable, unknown, non-numeric, or missing.
    """
    if not raw:
        return None
    s = raw.get("state")
    if s in (None, "unavailable", "unknown", "none"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _state_raw(raw: dict[str, Any] | None) -> str | None:
    """Return the raw string state or None."""
    if not raw:
        return None
    s = raw.get("state")
    return None if s in (None, "") else str(s)


def _is_unavailable(raw: dict[str, Any] | None) -> bool:
    """True if the entity is reporting an unusable state (unavailable/unknown)."""
    if not raw:
        return True
    return raw.get("state") in ("unavailable", "unknown", None, "")


def _last_changed(raw: dict[str, Any] | None) -> datetime | None:
    """Parse HA's last_changed (ISO8601) to a UTC datetime, or None."""
    if not raw:
        return None
    ts = raw.get("last_changed") or raw.get("last_updated")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hvac_action(raw: dict[str, Any] | None) -> str | None:
    """Extract the hvac_action attr from a climate entity's state dict."""
    if not raw:
        return None
    attrs = raw.get("attributes") or {}
    return attrs.get("hvac_action")


def _climate_target(raw: dict[str, Any] | None) -> float | None:
    """Extract the target temperature from a climate entity state dict."""
    if not raw:
        return None
    attrs = raw.get("attributes") or {}
    try:
        return float(attrs.get("temperature"))
    except (TypeError, ValueError):
        return None


def _seconds_since(then: datetime | None, now: datetime) -> float | None:
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds()


def _get_state(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    key: str,
) -> dict[str, Any] | None:
    """Fetch a state dict by registry key."""
    entry = registry.get(key)
    if not entry:
        return None
    return states.get(entry["entity_id"])


# ─────────────────────────────────────────────────────────────────────────────
# Flag functions
# ─────────────────────────────────────────────────────────────────────────────


def flag_canopy_offline(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Flag if any active canopy entity is unavailable. Marked `known=True` because
    the board's offline state is already acknowledged in each registry entry's
    known_state note.
    """
    offline: list[str] = []
    since: datetime | None = None
    for key, entry in registry.items():
        if entry.get("container") != "canopy":
            continue
        if not entry.get("active"):
            continue
        raw = states.get(entry["entity_id"])
        if _is_unavailable(raw):
            offline.append(key)
            lc = _last_changed(raw)
            if lc and (since is None or lc < since):
                since = lc
    if not offline:
        return None
    return {
        "flag": "canopy_offline",
        "level": "info",
        "since": since.isoformat() if since else None,
        "message": f"Canopy board offline: {', '.join(offline)} unavailable.",
        "known": True,
    }


def flag_bucket_phantom_heat(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """climate.bucket_rig calls for heat while outlet 2 draws 0W → phantom heating."""
    climate = _get_state(states, registry, "climate_bucket_rig")
    if not climate:
        return None
    if _hvac_action(climate) != "heating":
        return None

    power = _state_value(_get_state(states, registry, "outlet_2_bucket_power"))
    if power is None or power > 1.0:  # >1W means heater is actually drawing
        return None

    outlet_state = _get_state(states, registry, "outlet_2_bucket_power")
    since = _last_changed(outlet_state)
    secs = _seconds_since(since, now)
    if secs is not None and secs < 300:  # need ≥5min to distinguish from momentary 0W
        return None

    return {
        "flag": "bucket_phantom_heat",
        "level": "warn",
        "since": since.isoformat() if since else None,
        "message": (
            "climate.bucket_rig is calling heat but outlet 2 draws 0W. "
            "Heater unplugged, tripped, or outlet switch off. Consider set_hvac_mode: off."
        ),
        "known": False,
    }


def flag_tank_band_breach(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """tank_center outside climate.main_tank target ± flag_band."""
    raw = _get_state(states, registry, "tank_center")
    value = _state_value(raw)
    if value is None:
        return None

    entry = registry.get("tank_center", {})
    target = entry.get("target")
    band = entry.get("flag_band")

    # Prefer live climate target if available (follows runtime setpoint changes).
    climate_raw = _get_state(states, registry, "climate_main_tank")
    live_target = _climate_target(climate_raw)
    if live_target is not None:
        target = live_target

    if target is None or band is None:
        return None
    delta = value - target
    if abs(delta) <= band:
        return None

    level = "critical" if abs(delta) >= band * 2 else "warn"
    direction = "above" if delta > 0 else "below"
    return {
        "flag": "tank_band_breach",
        "level": level,
        "since": _last_changed(raw).isoformat() if _last_changed(raw) else None,
        "message": (
            f"Tank center {value:.2f}°F is {abs(delta):.2f}°F {direction} "
            f"target {target:.1f}°F (±{band}°F flag band)."
        ),
        "known": False,
    }


def flag_tds_out_of_range(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Any active TDS sensor reading outside its configured range. Returns a list
    because multiple tanks can flag simultaneously (Phase 2 readiness).
    """
    flags: list[dict[str, Any]] = []
    for key, entry in registry.items():
        if entry.get("role") != "tds" or not entry.get("active"):
            continue
        raw = states.get(entry["entity_id"])
        value = _state_value(raw)
        if value is None:
            continue
        rng = entry.get("range")
        if not rng:
            continue
        lo, hi = rng
        if lo <= value <= hi:
            continue
        direction = "above" if value > hi else "below"
        flags.append({
            "flag": f"tds_out_of_range:{key}",
            "level": "warn",
            "since": _last_changed(raw).isoformat() if _last_changed(raw) else None,
            "message": (
                f"{key} TDS {value:.0f} ppm is {direction} the ideal "
                f"range ({lo:.0f}-{hi:.0f} ppm)."
            ),
            "known": False,
        })
    return flags


def flag_stratification(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """abs(tank_center - tank_substrate) > 1°F → pump failure or dead zone."""
    center = _state_value(_get_state(states, registry, "tank_center"))
    substrate = _state_value(_get_state(states, registry, "tank_substrate"))
    if center is None or substrate is None:
        return None
    delta = abs(center - substrate)
    if delta <= 1.0:
        return None
    return {
        "flag": "stratification",
        "level": "warn" if delta < 2.0 else "critical",
        "since": None,
        "message": (
            f"Tank stratification: center {center:.2f}°F vs substrate "
            f"{substrate:.2f}°F ({delta:.2f}°F delta). Check pump flow."
        ),
        "known": False,
    }


def flag_heater_overdraw(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Heater power exceeds 150W — element fault or wrong heater."""
    raw = _get_state(states, registry, "heater_power")
    value = _state_value(raw)
    if value is None or value <= 150.0:
        return None
    return {
        "flag": "heater_overdraw",
        "level": "critical",
        "since": _last_changed(raw).isoformat() if _last_changed(raw) else None,
        "message": (
            f"Heater drawing {value:.1f}W exceeds 150W threshold. "
            "Possible element fault; investigate before long unattended."
        ),
        "known": False,
    }


def flag_basement_cold_drift(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Shelf ambient < 60°F → basement insulation losing vs outside, or cold snap."""
    raw = _get_state(states, registry, "shelf_ambient")
    value = _state_value(raw)
    if value is None or value >= 60.0:
        return None
    return {
        "flag": "basement_cold_drift",
        "level": "warn",
        "since": _last_changed(raw).isoformat() if _last_changed(raw) else None,
        "message": (
            f"Shelf ambient {value:.1f}°F below 60°F. Basement cold drift; "
            "heater duty will rise, check insulation + weather."
        ),
        "known": False,
    }


# Roles where HA state changes are continuous (analog sensors that always drift) —
# a stale timestamp here really does mean the device stopped reporting. Other roles
# are event-driven (switches flip when actuated, power meters report 0W until a
# load appears) and their "last_changed" legitimately ages without indicating a
# problem.
_CADENCE_ROLES = frozenset({
    "water_temp",
    "air_temp",
    "humidity",
    "illuminance",
    "tds",
    "ph",
    "soil_moisture",
    "auxiliary_temp",
})


def flag_sensor_stale(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Any active analog sensor whose last update is >30min old.

    Only checks roles in `_CADENCE_ROLES` — analog sensors that should update
    regularly because the underlying physical value drifts continuously. Event-
    driven entities (switches, outlets at idle, climate helpers, cameras) are
    skipped because "no change" is often the correct behavior, not a stale read.

    Returns a list — multiple sensors can go stale at once (wifi drop, board
    crash, integration down).
    """
    flags: list[dict[str, Any]] = []
    STALE_SECONDS = 1800  # 30 min

    for key, entry in registry.items():
        if not entry.get("active"):
            continue
        if entry.get("role") not in _CADENCE_ROLES:
            continue
        # External weather sensors round to whole-unit values (integer °F, integer %);
        # a state that sat at the same value for >30min legitimately reports an old
        # last_changed even though the upstream API is fresh. The upstream poll failure
        # case shows up as unavailable, not stale, so this exclusion is safe.
        if entry.get("container") == "external":
            continue
        # Skip known-offline entities — separate flag handles them.
        if entry.get("known_state") and "offline" in str(entry["known_state"]).lower():
            continue

        raw = states.get(entry["entity_id"])
        if raw is None or _is_unavailable(raw):
            continue  # offline handled elsewhere; this flag is specifically about staleness-while-available

        lc = _last_changed(raw)
        secs = _seconds_since(lc, now)
        if secs is None or secs <= STALE_SECONDS:
            continue

        # Illuminance sensors that delta-report legitimately idle in the dark: a
        # lux sensor sitting at its dark floor (~0 lx) stops emitting changes, which
        # reads as stale even though the board is healthy. When such a sensor is
        # stale *and* its last value is below the entry's configured dark threshold,
        # treat it as expected (info/known) rather than warn. A sensor frozen while
        # reading meaningful light is a genuine stall and still warns.
        dark_lux = entry.get("stale_dark_lux")
        value = _state_value(raw)
        if dark_lux is not None and value is not None and value < dark_lux:
            flags.append({
                "flag": f"sensor_stale:{key}",
                "level": "info",
                "since": lc.isoformat() if lc else None,
                "message": (
                    f"{key} ({entry['entity_id']}) idle at {value:.1f}lx for "
                    f"{secs / 60:.0f} min — expected: delta-reporting lux sensor sits "
                    f"at its dark floor and resumes when light returns."
                ),
                "known": True,
            })
            continue

        flags.append({
            "flag": f"sensor_stale:{key}",
            "level": "warn",
            "since": lc.isoformat() if lc else None,
            "message": (
                f"{key} ({entry['entity_id']}) has not updated in "
                f"{secs / 60:.0f} min; expected within 30 min."
            ),
            "known": False,
        })
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Flag registry — orchestrator runs all of these and collects non-None outputs
# ─────────────────────────────────────────────────────────────────────────────

# Functions returning a single dict or None
SINGLE_FLAGS: tuple[Callable, ...] = (
    flag_canopy_offline,
    flag_bucket_phantom_heat,
    flag_tank_band_breach,
    flag_stratification,
    flag_heater_overdraw,
    flag_basement_cold_drift,
)

# Functions returning a list of dicts (0..N)
MULTI_FLAGS: tuple[Callable, ...] = (
    flag_tds_out_of_range,
    flag_sensor_stale,
)


def evaluate_all(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Run all flag evaluators, return combined list sorted by level."""
    if now is None:
        now = datetime.now(timezone.utc)

    out: list[dict[str, Any]] = []
    for fn in SINGLE_FLAGS:
        try:
            result = fn(states, registry, now)
        except Exception as e:  # noqa: BLE001 — flags must not crash the tool
            out.append({
                "flag": f"flag_crash:{fn.__name__}",
                "level": "warn",
                "message": f"Flag evaluator raised: {e}",
                "known": False,
            })
            continue
        if result:
            out.append(result)

    for fn in MULTI_FLAGS:
        try:
            results = fn(states, registry, now) or []
        except Exception as e:  # noqa: BLE001
            out.append({
                "flag": f"flag_crash:{fn.__name__}",
                "level": "warn",
                "message": f"Flag evaluator raised: {e}",
                "known": False,
            })
            continue
        out.extend(results)

    # Sort by level severity (critical > warn > info), then by flag name for stability.
    order = {"critical": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: (order.get(f.get("level", "info"), 3), f.get("flag", "")))
    return out
