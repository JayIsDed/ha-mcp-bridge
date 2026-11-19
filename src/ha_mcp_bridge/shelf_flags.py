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
            continue  # flag_entity_offline / flag_canopy_offline report these; this one is staleness-while-available

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


# Unavailable/unknown this long before it counts as dark. Measured blips: the
# ESPHome boards drop to unavailable for ~0.1 s on a reconnect and the smart
# plugs showed none in a week, while a real outage runs for days. An HA restart
# clears in a minute or two. 10 min is far above the noise and still well inside
# the hours a cooling tank gives you.
OFFLINE_AFTER_S = 600

_LEVEL_RANK = {"info": 0, "warn": 1, "critical": 2}


def fmt_age(secs: float) -> str:
    if secs < 7200:
        return f"{secs / 60:.0f} min"
    if secs < 172800:
        return f"{secs / 3600:.0f} h"
    return f"{secs / 86400:.1f} days"


def flag_entity_offline(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Any active entity that is unavailable, unknown or null for longer than
    OFFLINE_AFTER_S, or that HA returned no state for at all.

    Level comes from the entry's `offline_level` (default warn); the probes,
    heater and pump the tank depends on are critical. An entry's
    `offline_after_s` replaces OFFLINE_AFTER_S for it. The canopy board is left to
    flag_canopy_offline, and entries whose known_state says offline are skipped,
    as sensor_stale does.

    `since` is when the entity went dark (its last_changed), so the age is real.

    One device, one flag: an entry whose `part_of` names another entry (a
    plug's power and voltage readings, `part_of` its switch) is folded into
    that entry's flag when both are dark, at the higher of the two levels. A
    reading that goes dark on its own still flags by itself.
    """
    found: dict[str, dict[str, Any]] = {}
    for key, entry in registry.items():
        if not entry.get("active"):
            continue
        if entry.get("container") == "canopy":
            continue
        if entry.get("known_state") and "offline" in str(entry["known_state"]).lower():
            continue

        eid = entry["entity_id"]
        raw = states.get(eid)
        if raw is not None and not _is_unavailable(raw):
            continue

        if raw is None:
            since = None
            detail = "has no state in HA (entity missing, or its read failed)"
        else:
            since = _last_changed(raw)
            secs = _seconds_since(since, now)
            if secs is not None and secs < entry.get("offline_after_s", OFFLINE_AFTER_S):
                continue
            shown = raw.get("state") or "null"
            detail = f"has been {shown}" + (f" for {fmt_age(secs)}" if secs is not None else "")
        found[key] = {
            "level": entry.get("offline_level") or "warn",
            "since": since,
            "detail": f"{key} ({eid}) {detail}.",
            "also": [],
        }

    for key in list(found):
        parent = registry[key].get("part_of")
        if parent and parent != key and parent in found:
            child = found.pop(key)
            found[parent]["also"].append(f"{key} ({registry[key]['entity_id']})")
            if _LEVEL_RANK[child["level"]] > _LEVEL_RANK[found[parent]["level"]]:
                found[parent]["level"] = child["level"]

    flags: list[dict[str, Any]] = []
    for key, f in found.items():
        message = f["detail"]
        if f["also"]:
            message += f" Same device, also dark: {', '.join(f['also'])}."
        if f["level"] == "critical":
            message += " Life support: nothing is watching or driving this part of the tank."
        flags.append({
            "flag": f"entity_offline:{key}",
            "level": f["level"],
            "since": f["since"].isoformat() if f["since"] else None,
            "message": message,
            "known": False,
        })
    return flags


# How long a heater without power must stay that way before it counts as
# failed. Measured over 528 normal relay-on cycles (2.5 months of recorded
# history): the heater draws within 7 s of the plug switching on, and its own
# built-in thermostat then cycles it off for up to 5.7 min at a time (p99
# 5.5 min) while the plug stays on and the tank sits in band. So:
# - the heater never drew since the plug switched on, the plug is off or dark,
#   or the tank is already cold: 5 min, far above the 7 s switch-on lag;
# - it drew and then stopped while the tank is in band, which is what a normal
#   cycle's tail looks like: 15 min, 2.6x the longest normal tail. A heater
#   that dies mid-cycle is still caught at 15 min, or within 5 min of the tank
#   falling below the flag band, whichever comes first.
HEATER_HOLD_S = 300
HEATER_TAIL_HOLD_S = 900


def flag_tank_heater_not_heating(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """The main tank wants heat and the heater has no power.

    "Wants heat": climate_main_tank is in heat mode and either reports
    hvac_action=heating, or tank_center sits below target by more than its
    flag_band. The second arm matters because a thermostat whose heater switch
    has gone unavailable reports idle, not heating, while the tank cools.
    `flag_band` (not the hysteresis `band`) keeps clear of the moment between
    the tank crossing the on-threshold and the thermostat flipping the switch.

    "No power": the heater switch is not on (off, unavailable, missing), or it
    is on and heater_power reads 1 W or less. The plug must have sat that way
    (the later of the switch's and the power reading's last change) for
    HEATER_HOLD_S, or HEATER_TAIL_HOLD_S when the heater drew since the plug
    switched on and the tank is in band: a heater's own thermostat cuts out for
    a few minutes at the end of every normal cycle.

    Critical when the plug is off or drawing nothing; warn when the plug is on
    but its power reading is dark, since then the heat can't be confirmed
    either way.
    """
    climate = _get_state(states, registry, "climate_main_tank")
    if not climate or climate.get("state") != "heat":
        return None

    power_raw = _get_state(states, registry, "heater_power")
    power = _state_value(power_raw)
    if power is not None and power > 1.0:
        return None

    tank = _state_value(_get_state(states, registry, "tank_center"))
    tank_entry = registry.get("tank_center", {})
    target = _climate_target(climate)
    if target is None:
        target = tank_entry.get("target")
    flag_band = tank_entry.get("flag_band")

    calling = _hvac_action(climate) == "heating"
    cold = (
        tank is not None
        and target is not None
        and flag_band is not None
        and tank < target - flag_band
    )
    if not (calling or cold):
        return None

    switch_raw = _get_state(states, registry, "heater_switch")
    plug = _state_raw(switch_raw) if switch_raw else None
    switch_changed, power_changed = _last_changed(switch_raw), _last_changed(power_raw)
    changes = [t for t in (switch_changed, power_changed) if t]
    since = max(changes) if changes else None
    # The power reading changed after the plug switched on and now reads ~0:
    # the heater drew, then stopped. With the tank in band that is the shape of
    # a normal cycle's tail, so it gets the longer hold.
    drew_then_stopped = (
        plug == "on"
        and power is not None
        and switch_changed is not None
        and power_changed is not None
        and power_changed > switch_changed
    )
    hold = HEATER_TAIL_HOLD_S if drew_then_stopped and not cold else HEATER_HOLD_S
    secs = _seconds_since(since, now)
    if secs is not None and secs < hold:
        return None

    lead = (
        "Main tank thermostat is calling for heat" if calling
        else f"Main tank is {tank:.1f}°F, {target - tank:.1f}°F below target {target:.1f}°F"
    )
    if plug == "on" and power is None:
        level = "warn"
        cause = "the heater plug is on but its power reading is dark, so the heat can't be confirmed"
    elif plug == "on":
        level = "critical"
        cause = f"the heater plug is on and drawing {power:.1f} W"
        if secs is not None:
            cause += f" for {fmt_age(secs)}"
    else:
        level = "critical"
        cause = f"the heater plug is {plug or 'missing'}"
    return {
        "flag": "tank_heater_not_heating",
        "level": level,
        "since": since.isoformat() if since else None,
        "message": f"{lead}, but {cause}.",
        "known": False,
    }


# A running pump never drew less than 2.3 W in four months of recorded history
# (every hour's minimum, mid-July to mid-November), so 1 W or less means it has
# stopped. The hold matches the heater's: long enough that a pump switched on a
# moment ago and a meter catching up don't flag.
PUMP_STOPPED_W = 1.0
PUMP_HOLD_S = 300


def flag_pump_not_running(
    states: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """The tank pump's outlet is switched off, or on and drawing ~0 W, for
    PUMP_HOLD_S. Critical: the pump is life support.

    A dark outlet (unavailable, unknown or missing) is left to
    flag_entity_offline, which reports it at the pump entries' own
    offline_level and offline_after_s, so one fault raises one flag.
    """
    entry = registry.get("outlet_1_pump_switch")
    if not entry or not entry.get("active"):
        return None
    switch_raw = _get_state(states, registry, "outlet_1_pump_switch")
    power_raw = _get_state(states, registry, "outlet_1_pump_power")
    plug = _state_raw(switch_raw)
    power = _state_value(power_raw)

    if plug == "off":
        since = _last_changed(switch_raw)
        cause = "is switched off"
    elif plug == "on" and power is not None and power <= PUMP_STOPPED_W:
        changes = [t for t in (_last_changed(switch_raw), _last_changed(power_raw)) if t]
        since = max(changes) if changes else None
        cause = f"is on but drawing {power:.1f} W"
    else:
        return None  # running, or dark: flag_entity_offline reports dark

    secs = _seconds_since(since, now)
    if secs is not None and secs < PUMP_HOLD_S:
        return None
    age = f" for {fmt_age(secs)}" if secs is not None else ""
    return {
        "flag": "pump_not_running",
        "level": "critical",
        "since": since.isoformat() if since else None,
        "message": (
            f"The tank pump's outlet ({entry['entity_id']}) {cause}{age}. "
            "Life support: nothing is moving water through the tank."
        ),
        "known": False,
    }


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
    flag_tank_heater_not_heating,
    flag_pump_not_running,
)

# Functions returning a list of dicts (0..N)
MULTI_FLAGS: tuple[Callable, ...] = (
    flag_tds_out_of_range,
    flag_sensor_stale,
    flag_entity_offline,
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
