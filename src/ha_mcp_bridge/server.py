from __future__ import annotations

import fnmatch
import json
import logging
import os
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from PIL import Image as PILImage

from .ha_client import HAClient, HAError
from .influx_client import InfluxClient, InfluxError
from .archbox_health import build_archbox_full, build_archbox_pulse
from .shelf_health import build_pulse, build_snapshot
from .types import BinnedPoint, EntityInfo, EntityState, HistoryPoint


def _load_env() -> None:
    # Honor a .env next to the project if present; real deploys use Infisical-synced env.
    here = Path(__file__).resolve().parent.parent.parent
    load_dotenv(here / ".env", override=False)
    load_dotenv(override=False)


_load_env()

HA_URL = os.environ.get("HA_URL", "http://192.168.8.127:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_TIMEOUT = float(os.environ.get("HA_HTTP_TIMEOUT", "15"))
HA_HISTORY_MAX_HOURS = int(os.environ.get("HA_HISTORY_MAX_HOURS", "168"))

INFLUX_URL = os.environ.get("INFLUX_URL", "")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "homelab")
INFLUX_TIMEOUT = float(os.environ.get("INFLUX_HTTP_TIMEOUT", "30"))
# Separate read-only token scoped to the `hosts` bucket only. INFLUX_TOKEN
# cannot see that bucket (returns 404), and broadening it would hand this
# bridge more than it needs.
INFLUX_HOSTS_TOKEN = os.environ.get("INFLUX_HOSTS_TOKEN", "")

# Responses larger than this serialize to more than a single pipe buffer (64KB default
# on Linux) and will hang the MCP stdio transport if the client isn't draining fast
# enough. Guard at the tool layer so we return an actionable error instead of stalling.
MAX_RESPONSE_BYTES = int(os.environ.get("HA_MCP_MAX_RESPONSE_BYTES", "120000"))

# Camera snapshot defaults — resize + recompress so 4K frames don't hang stdio.
CAMERA_MAX_WIDTH = int(os.environ.get("HA_MCP_CAMERA_MAX_WIDTH", "1024"))
CAMERA_JPEG_QUALITY = int(os.environ.get("HA_MCP_CAMERA_JPEG_QUALITY", "70"))


def _parse_csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


# Allowlist for ha_call_service. Both lists must be non-empty for the tool to operate
# (default-deny). Patterns use fnmatch glob syntax — e.g. "light.*" matches all light
# services; "*plant_shelf*" matches any entity with that substring.
ALLOWED_SERVICES = _parse_csv_env("HA_MCP_ALLOWED_SERVICES")
ALLOWED_ENTITY_PATTERNS = _parse_csv_env("HA_MCP_ALLOWED_ENTITY_PATTERNS")

# Logging: warnings go to stderr by default (security denials need to be visible).
# Setting HA_MCP_DEBUG_LOG routes full debug output to that file.
_DEBUG_LOG = os.environ.get("HA_MCP_DEBUG_LOG", "")
if _DEBUG_LOG:
    logging.basicConfig(
        filename=_DEBUG_LOG,
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
    )
else:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s ha-mcp-bridge: %(message)s",
    )
log = logging.getLogger("ha-mcp-bridge")

if not HA_TOKEN:
    # Don't crash at import — MCP handshake may still work, but tool calls will fail loud.
    # This lets `ha-mcp-bridge --help` style probes work without a token present.
    print(
        "WARN: HA_TOKEN not set. Tool calls will fail until it is populated.",
        file=sys.stderr,
    )


mcp = FastMCP("ha-mcp-bridge")


def _client() -> HAClient:
    return HAClient(HA_URL, HA_TOKEN, timeout=HA_TIMEOUT)


def _influx() -> InfluxClient:
    return InfluxClient(INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, timeout=INFLUX_TIMEOUT)


def _size_error(tool: str, size_bytes: int, hint: str) -> dict[str, Any]:
    """Build the standard oversize-response error payload."""
    log.warning("size guard: %s produced %d bytes (limit %d)", tool, size_bytes, MAX_RESPONSE_BYTES)
    return {
        "error": "response_too_large",
        "tool": tool,
        "size_bytes": size_bytes,
        "limit_bytes": MAX_RESPONSE_BYTES,
        "hint": hint,
    }


def _guard_dict(tool: str, data: dict[str, Any], hint: str) -> dict[str, Any]:
    size = len(json.dumps(data, default=str))
    if size > MAX_RESPONSE_BYTES:
        return _size_error(tool, size, hint)
    return data


def _guard_list(tool: str, data: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    size = len(json.dumps(data, default=str))
    if size > MAX_RESPONSE_BYTES:
        return [_size_error(tool, size, hint)]
    return data


def _is_service_allowed(service_id: str) -> bool:
    return any(fnmatch.fnmatchcase(service_id, p) for p in ALLOWED_SERVICES)


def _is_entity_allowed(entity_id: str) -> bool:
    return any(fnmatch.fnmatchcase(entity_id, p) for p in ALLOWED_ENTITY_PATTERNS)


@mcp.tool()
async def ha_list_entities(domain: str | None = None) -> list[dict]:
    """List Home Assistant entities, optionally filtered by domain.

    Args:
        domain: Optional domain prefix filter (e.g. "sensor", "switch", "binary_sensor",
            "light", "climate"). When omitted, returns every entity known to HA.

    Returns:
        List of {entity_id, friendly_name, state, last_changed, unit_of_measurement,
        device_class}. Compact shape so Claude can scan many entries without flooding context.
    """
    async with _client() as ha:
        try:
            raw = await ha.list_states()
        except HAError as e:
            return [{"error": str(e)}]

    if domain:
        prefix = f"{domain}."
        raw = [e for e in raw if e.get("entity_id", "").startswith(prefix)]

    result = [EntityInfo.from_ha(e).model_dump() for e in raw]
    hint = (
        "Filter by domain (e.g. domain='sensor') to narrow the list. "
        f"Unfiltered HA instances with many entities easily exceed the "
        f"{MAX_RESPONSE_BYTES}-byte response cap."
    )
    return _guard_list("ha_list_entities", result, hint)


@mcp.tool()
async def ha_state(entity_id: str) -> dict:
    """Get the current state and attributes of a single Home Assistant entity.

    Args:
        entity_id: Fully-qualified entity id, e.g. "sensor.plant_shelf_temperatures_tank_center".
            Slug is derived from the ESPHome friendly_name (slugified including punctuation —
            "Probe 4 (spare)" becomes "probe_4_spare"). Call ha_list_entities first if unsure.

    Returns:
        {entity_id, state, attributes, last_changed, last_updated}. Attributes include
        unit_of_measurement, friendly_name, device_class, etc. depending on entity type.
        Returns {error: ...} if the entity is not found or HA is unreachable.
    """
    async with _client() as ha:
        try:
            raw = await ha.get_state(entity_id)
        except HAError as e:
            return {"error": str(e)}

    if raw is None:
        return {"error": f"entity not found: {entity_id}"}
    return EntityState.from_ha(raw).model_dump()


@mcp.tool()
async def ha_camera_snapshot(
    entity_id: str,
    max_width: int | None = None,
    quality: int | None = None,
) -> Any:
    """Fetch the latest frame from a Home Assistant camera entity as a viewable image.

    Returned as an MCP image content block so Claude can see the frame directly
    (plant growth check, leaf pose, algae bloom, tank clarity, grow-light coverage).
    The raw frame is resized + JPEG-recompressed before return to stay under the
    stdio pipe buffer — unresized 4K snapshots would hang the transport.

    Args:
        entity_id: Fully-qualified camera entity, e.g. "camera.reolink_e1_zoom".
            Must start with "camera." — any other domain is refused.
        max_width: Resize so the longest edge <= this many px. Default from env
            (HA_MCP_CAMERA_MAX_WIDTH=1024). Aspect ratio preserved.
        quality: JPEG quality 1-95. Default from env (HA_MCP_CAMERA_JPEG_QUALITY=70).
            70 is visually indistinguishable from 90 at 1024px for most scenes.

    Returns:
        Image content (mime image/jpeg) on success — Claude can view it directly.
        {error: ...} dict on failure: wrong entity domain, unreachable camera,
        or post-resize image still exceeds the response size cap.
    """
    if not entity_id.startswith("camera."):
        return {
            "error": "invalid_entity_domain",
            "hint": f"ha_camera_snapshot only operates on camera.* entities, got: {entity_id}",
        }

    max_w = int(max_width) if max_width is not None else CAMERA_MAX_WIDTH
    q = max(1, min(95, int(quality) if quality is not None else CAMERA_JPEG_QUALITY))

    async with _client() as ha:
        try:
            raw = await ha.get_camera_snapshot(entity_id)
        except HAError as e:
            return {"error": str(e), "entity_id": entity_id}

    try:
        img = PILImage.open(BytesIO(raw))
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)), PILImage.LANCZOS)
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=q, optimize=True)
        processed = buf.getvalue()
    except Exception as e:
        return {"error": f"image_processing_failed: {e}", "entity_id": entity_id}

    if len(processed) > MAX_RESPONSE_BYTES:
        return {
            "error": "image_too_large",
            "entity_id": entity_id,
            "size_bytes": len(processed),
            "limit_bytes": MAX_RESPONSE_BYTES,
            "hint": "Lower max_width or quality parameters.",
        }

    return Image(data=processed, format="jpeg")


@mcp.tool()
async def ha_history(entity_id: str, hours: int = 24) -> list[dict]:
    """Fetch state-change history for an entity over a time window.

    Args:
        entity_id: Fully-qualified entity id.
        hours: Window length in hours, 1..168 (1h to 1 week). Clamped if out of range.

    Returns:
        Time-ordered list of {state, last_changed, unit_of_measurement}. Uses HA's
        minimal_response mode so payload scales with change frequency, not sample rate.
        Returns [{error: ...}] on failure.
    """
    hours = max(1, min(HA_HISTORY_MAX_HOURS, int(hours)))
    async with _client() as ha:
        try:
            raw = await ha.get_history(entity_id, hours)
        except HAError as e:
            return [{"error": str(e)}]

    result = [HistoryPoint.from_ha(p).model_dump() for p in raw]
    hint = (
        f"Shorten the window (hours=), or switch to ha_history_binned to bucket a "
        f"flappy sensor. Current response exceeds {MAX_RESPONSE_BYTES} bytes."
    )
    return _guard_list("ha_history", result, hint)


@mcp.tool()
async def ha_query_grouped(entity_ids: list[str], hours: int = 1) -> dict:
    """Fetch history for multiple entities in one call. Useful for correlated analysis.

    Args:
        entity_ids: List of fully-qualified entity ids.
        hours: Window length in hours, 1..168.

    Returns:
        {entity_id: [HistoryPoint, ...]} keyed by entity. Missing/errored entities
        show up with a single {error: ...} entry in their list. Issues requests
        concurrently, so wall-clock cost scales with slowest entity, not sum.
    """
    import asyncio

    hours = max(1, min(HA_HISTORY_MAX_HOURS, int(hours)))

    async def one(ha: HAClient, eid: str) -> tuple[str, list[dict]]:
        try:
            raw = await ha.get_history(eid, hours)
            return eid, [HistoryPoint.from_ha(p).model_dump() for p in raw]
        except HAError as e:
            return eid, [{"error": str(e)}]

    async with _client() as ha:
        results = await asyncio.gather(*(one(ha, eid) for eid in entity_ids))

    hint = (
        "Narrow the window (hours=), drop entities from entity_ids, or call "
        "ha_history_binned per-entity with a larger bin_minutes to pre-aggregate."
    )
    return _guard_dict("ha_query_grouped", dict(results), hint)


@mcp.tool()
async def ha_history_binned(
    entity_id: str,
    hours: int = 24,
    bin_minutes: int = 15,
    aggregation: str = "mean",
) -> list[dict]:
    """Fetch state history binned into time buckets with aggregation.

    Raw ha_history can return hundreds of rows for sensors that flap between quantization
    steps. This endpoint buckets them so summary analysis costs a fraction of the context.

    Args:
        entity_id: Fully-qualified entity id.
        hours: Window length, 1..168. Clamped.
        bin_minutes: Bucket size in minutes. Default 15.
        aggregation: "mean" | "min" | "max" | "first" | "last". Default "mean".
            Non-numeric states are skipped silently when aggregating numeric values;
            the bin still shows n=count of samples landed in it.

    Returns:
        [{bucket_start, n, mean?, min?, max?, first?, last?}]. Fields present depend on
        aggregation — all bins include n and bucket_start; the requested aggregation
        field is always populated when any numeric sample fell in the bucket.
        Empty buckets (zero state changes) are omitted.
    """
    from datetime import datetime as _dt

    hours = max(1, min(HA_HISTORY_MAX_HOURS, int(hours)))
    bin_minutes = max(1, int(bin_minutes))
    agg = aggregation.lower()
    if agg not in {"mean", "min", "max", "first", "last"}:
        return [{"error": f"unknown aggregation: {aggregation}"}]

    async with _client() as ha:
        try:
            raw = await ha.get_history(entity_id, hours)
        except HAError as e:
            return [{"error": str(e)}]

    buckets: dict[str, list[tuple[str, float]]] = {}
    for point in raw:
        ts = point.get("last_changed")
        state = point.get("state")
        if ts is None or state is None:
            continue
        try:
            value = float(state)
        except (TypeError, ValueError):
            continue
        t = _dt.fromisoformat(ts.replace("Z", "+00:00"))
        # Floor to bin boundary based on minutes-since-epoch
        floor_min = (t.hour * 60 + t.minute) // bin_minutes * bin_minutes
        bucket_t = t.replace(hour=floor_min // 60, minute=floor_min % 60, second=0, microsecond=0)
        buckets.setdefault(bucket_t.isoformat(), []).append((ts, value))

    out: list[BinnedPoint] = []
    for bucket_start in sorted(buckets):
        samples = buckets[bucket_start]
        values = [v for _, v in samples]
        point = BinnedPoint(bucket_start=bucket_start, n=len(values))
        if agg == "mean":
            point.mean = sum(values) / len(values)
        elif agg == "min":
            point.min = min(values)
        elif agg == "max":
            point.max = max(values)
        elif agg == "first":
            point.first = samples[0][1]
        elif agg == "last":
            point.last = samples[-1][1]
        out.append(point)

    result = [p.model_dump(exclude_none=True) for p in out]
    hint = (
        "Raise bin_minutes or shorten the window (hours=). A 24h window at "
        "bin_minutes=1 can emit 1440 buckets."
    )
    return _guard_list("ha_history_binned", result, hint)


@mcp.tool()
async def ha_logbook(hours: int = 1, entity_id: str | None = None) -> list[dict]:
    """Fetch HA's human-readable event log over a time window.

    Logbook captures state changes HA considers noteworthy: automations firing,
    devices turning on/off, user actions, scripts running. Cleaner than raw
    history for "what happened around time X" questions.

    Args:
        hours: Window length in hours, 1..168 (clamped). Default 1h.
        entity_id: Optional — filter to a single entity's events. Omit for all.

    Returns:
        Time-ordered list of {when, name, message?, state?, entity_id?, domain?}.
        `message` is the human phrase (e.g. "turned on", "executed automation");
        numeric sensors typically don't appear unless explicitly logbook-tracked.
        Returns [{error: ...}] on failure or [] if nothing happened in the window.
    """
    hours = max(1, min(HA_HISTORY_MAX_HOURS, int(hours)))
    async with _client() as ha:
        try:
            raw = await ha.get_logbook(hours, entity_id)
        except HAError as e:
            return [{"error": str(e)}]

    hint = (
        "Shorten the window (hours=) or filter to a specific entity_id. "
        "Busy automations can produce hundreds of entries."
    )
    return _guard_list("ha_logbook", raw, hint)


@mcp.tool()
async def ha_press_button(entity_id: str) -> dict:
    """Press a Home Assistant button entity (e.g. ESPHome restart buttons).

    Narrow-scope actuation. Only acts on button-domain entities; for anything broader
    you'd want a full service-call tool (not exposed here by design).

    Args:
        entity_id: Fully-qualified button entity, e.g. "button.plant_shelf_temperatures_restart".

    Returns:
        {ok: true, entity_id, result} on success, {error: ...} on failure.
        HA's response body is included under "result" for inspection.
    """
    if not entity_id.startswith("button."):
        return {"error": f"ha_press_button only operates on button.* entities, got: {entity_id}"}

    async with _client() as ha:
        try:
            result = await ha.call_service("button", "press", {"entity_id": entity_id})
        except HAError as e:
            return {"error": str(e)}

    return {"ok": True, "entity_id": entity_id, "result": result}


@mcp.tool()
async def ha_call_service(domain: str, service: str, data: dict | None = None) -> dict:
    """Call an allowlisted Home Assistant service. Default-deny — both env allowlists
    must be set for this tool to operate.

    Configured via two env vars (fnmatch glob patterns, comma-separated):
      HA_MCP_ALLOWED_SERVICES          e.g. "light.*,switch.turn_on,switch.turn_off,scene.turn_on"
      HA_MCP_ALLOWED_ENTITY_PATTERNS   e.g. "*plant_shelf*,light.grow_*"  (or "*" to allow any)

    Both gates are evaluated: the service id `{domain}.{service}` must match at least one
    service pattern, AND every entity_id in `data` must match at least one entity pattern.
    Area/device-level targeting is not permitted — call with explicit entity_id(s).

    Denials are logged to stderr so the operator can audit attempted out-of-scope calls.

    Args:
        domain: Service domain, e.g. "light", "switch", "scene", "fan".
        service: Service name, e.g. "turn_on", "toggle", "set_percentage".
        data: Service payload. MUST contain "entity_id" (string or list of strings).
            Additional keys are passed through (e.g. brightness_pct, rgb_color, percentage).

    Returns:
        {ok: true, service, entity_ids, result} on success.
        {error: "ha_call_service disabled" | "service_not_allowed" | "entity_not_allowed"
         | "entity_required" | <HA error>, ...diagnostic fields} on failure.
    """
    if not ALLOWED_SERVICES or not ALLOWED_ENTITY_PATTERNS:
        return {
            "error": "ha_call_service disabled",
            "hint": (
                "Set both HA_MCP_ALLOWED_SERVICES and HA_MCP_ALLOWED_ENTITY_PATTERNS "
                "in env to enable. Both accept comma-separated fnmatch glob patterns."
            ),
            "allowed_services_set": bool(ALLOWED_SERVICES),
            "allowed_entity_patterns_set": bool(ALLOWED_ENTITY_PATTERNS),
        }

    service_id = f"{domain}.{service}"
    if not _is_service_allowed(service_id):
        log.warning("ha_call_service DENIED service: %s", service_id)
        return {
            "error": "service_not_allowed",
            "service": service_id,
            "allowed_services": ALLOWED_SERVICES,
        }

    payload = dict(data or {})
    raw_entity = payload.get("entity_id")
    if raw_entity is None:
        if "area_id" in payload or "device_id" in payload:
            return {
                "error": "entity_required",
                "hint": "Area/device-level targeting is not permitted. Call with explicit entity_id.",
            }
        return {
            "error": "entity_required",
            "hint": "data must include entity_id (string or list of strings).",
        }

    entity_ids = [raw_entity] if isinstance(raw_entity, str) else list(raw_entity)
    for eid in entity_ids:
        if not _is_entity_allowed(eid):
            log.warning(
                "ha_call_service DENIED entity: %s (service %s)", eid, service_id
            )
            return {
                "error": "entity_not_allowed",
                "entity_id": eid,
                "service": service_id,
                "allowed_entity_patterns": ALLOWED_ENTITY_PATTERNS,
            }

    async with _client() as ha:
        try:
            result = await ha.call_service(domain, service, payload)
        except HAError as e:
            return {"error": str(e), "service": service_id, "entity_ids": entity_ids}

    log.info("ha_call_service OK: %s on %s", service_id, entity_ids)
    return {"ok": True, "service": service_id, "entity_ids": entity_ids, "result": result}


@mcp.tool()
async def influx_flux(query: str) -> dict:
    """Run a Flux query against the homelab InfluxDB 2.x instance (read-only scope).

    Bucket access is scoped to `homeassistant` (all HA entity history, infinite retention)
    and `sensors` (homelab sensor pipeline from Telegraf/MQTT). No write access.

    Example queries:

      Last 1h of tank center temp:
        from(bucket: "homeassistant")
          |> range(start: -1h)
          |> filter(fn: (r) => r.entity_id == "plant_shelf_temperatures_tank_center"
                            and r._field == "value")

      Hourly mean over last 7d for all plant shelf sensors:
        from(bucket: "homeassistant")
          |> range(start: -7d)
          |> filter(fn: (r) => r.entity_id =~ /^plant_shelf_temperatures_/
                            and r._field == "value")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)

      Heater duty cycle via threshold crossings (requires tank temp + timestamps):
        from(bucket: "homeassistant")
          |> range(start: -24h)
          |> filter(fn: (r) => r.entity_id == "plant_shelf_temperatures_tank_center"
                            and r._field == "value")
          |> derivative(unit: 1m, nonNegative: false)

    Args:
        query: Full Flux query string. Must start with `from(bucket: ...)` or a variable.

    Returns:
        {ok: true, rows: [{col: val, ...}, ...], n: int} on success.
        {error: ...} on failure — check bucket spelling, token scope, and query syntax.
        Numeric types are coerced (float/int/bool); strings stay as strings.
        Each row carries `_table` for grouping when the query emits multiple tables.
    """
    if not INFLUX_URL or not INFLUX_TOKEN:
        return {"error": "INFLUX_URL and INFLUX_TOKEN must be set in env/.env."}

    async with _influx() as influx:
        try:
            rows = await influx.query(query)
        except InfluxError as e:
            return {"error": str(e)}

    hint = (
        "Add an aggregateWindow() step, narrow the range(), or filter tighter. "
        "Large raw CSV responses hang the MCP stdio transport."
    )
    return _guard_dict("influx_flux", {"ok": True, "rows": rows, "n": len(rows)}, hint)


@mcp.tool()
async def ha_template(template: str) -> dict:
    """Render a Jinja2 template against the live Home Assistant state.

    Useful for computed queries that the state API doesn't expose directly:
      - Heater duty cycle: {% set heater = states('sensor.heater_power') | float(0) %}...
      - Area rollups: {{ expand(area_entities('basement')) | selectattr(...) | list | count }}
      - Cross-entity math: {{ (states('sensor.a') | float) - (states('sensor.b') | float) }}

    Args:
        template: Jinja2 template string. HA's templating sandbox applies — no file/shell access.

    Returns:
        {ok: true, rendered: <text>} on success, {error: ...} on failure.
        Rendered output is always returned as a string; coerce numerics client-side.
    """
    async with _client() as ha:
        try:
            rendered = await ha.render_template(template)
        except HAError as e:
            return {"error": str(e)}

    return {"ok": True, "rendered": rendered}


@mcp.tool()
async def shelf_health_full() -> dict:
    """Full calibration-shelf health snapshot in one call.

    Replaces the 20-call parallel ha_state sweep that a manual morning check
    otherwise needs. Returns live values grouped by category (thermal, chemistry,
    power, climate, light, camera, weather, infrastructure), plus a list of
    anomaly flags evaluated against the registry-defined bounds.

    Categories populated depend on which entities are currently `active` in the
    shelf registry (src/ha_mcp_bridge/shelf_registry.py). Entities gated off
    (e.g. TDS probe not physically plugged in) are skipped silently. Adding a
    new sensor = one line in the registry; this tool picks it up on next call.

    Flags surface issues that a raw state dump would hide:
      - canopy_offline (known-off board still dark)
      - bucket_phantom_heat (climate calling heat with 0W actual draw)
      - tank_band_breach (tank_center outside climate target ± flag_band)
      - tds_out_of_range (chemistry drift beyond organism-safe bounds)
      - stratification (tank top vs substrate delta >1°F)
      - heater_overdraw (>150W = element fault)
      - basement_cold_drift (ambient <60°F)
      - sensor_stale (active sensor >30min without update)

    Returns:
        {timestamp, summary:{active_entities, offline, flags:{critical,warn,info}},
         flags:[...], thermal:{...}, chemistry:{...}, power:{...}, climate:{...},
         light:{...}, weather:{...}, infrastructure:{...}}
        Sections are omitted when empty. Typical response ≈ 2-3 KB.
    """
    async with _client() as ha:
        try:
            snapshot = await build_snapshot(ha)
        except HAError as e:
            return {"error": str(e)}

    hint = (
        "Registry grew too large for the response cap. Split into category-specific "
        "tools or filter the registry before composing the snapshot."
    )
    return _guard_dict("shelf_health_full", snapshot, hint)


@mcp.tool()
async def shelf_pulse() -> dict:
    """Quick pulse check — vitals + anomaly flags in one small payload.

    The mid-day "anything wrong + what are the key numbers" check. Returns a
    compact `vitals` block of the sensors that matter most on a pulse:

      - tank_center + tank_target + tank_delta (primary thermal)
      - heater_power + heater_calling (is L1 firing as expected)
      - tank_substrate (stratification sanity)
      - tds_tank + tds_status (chemistry sanity)
      - shelf_ambient + outside_temp + basement_delta (envelope)
      - forecast_5d_min_low (cold-snap horizon)
      - l0_power (total shelf + printer draw)

    Plus the full anomaly flag list (same evaluators as shelf_health_full). Use
    this whenever you want a one-call health pulse without the full category
    breakdown — ~1-1.5 KB response vs ~5 KB for shelf_health_full.

    Returns:
        {timestamp, summary:{critical, warn, info}, vitals:{...numbers...},
         flags:[{flag, level, since?, message, known}, ...]}
    """
    async with _client() as ha:
        try:
            result = await build_pulse(ha)
        except HAError as e:
            return {"error": str(e)}
    return result


@mcp.tool()
async def archbox_pulse() -> dict:
    """One-call health bundle for the archbox (jay's 7950X / RTX 3090 rig).

    The workstation equivalent of shelf_pulse. Prefer this over firing several
    ha_state calls whenever the question is "how's the archbox" / "is it hot" /
    "what's it drawing" / "is it even on". Response ~1 KB.

    vitals:
      thermals  cpu_temp, gpu_temp, gpu_hotspot, gpu_vram, gpu_vrm,
                coolant_temp (water loop), nvme_temp (hottest of 4)
      load      cpu_load, gpu_load, memory_used
      power     gpu_power, wall_power (WHOLE system at the plug, incl. PSU
                losses), line_voltage, today_kwh, month_kwh
      state     power_switch (WoL on / authenticated poweroff), mains_switch
      derived   non_gpu_power (wall - gpu), gpu_over_coolant (how hard the loop
                is working), hotspot_delta (core-to-hotspot; a high value is
                the classic degraded-paste tell)

    flags: per-metric warn/critical thresholds tuned for THIS hardware, plus
    archbox_offline, sensor_unavailable:*, mains_off_while_on, and
    hotspot_delta_high.

    Data path: telegraf (archbox) -> InfluxDB "hosts" -> HA influxdb sensors.
    If the box is off, telemetry goes stale and `online` reports false — wake it
    with switch.archbox. Full hardware notes: ai-lab/docs/archbox-monitoring.md.

    Returns:
        {timestamp, online, summary:{critical, warn, info},
         vitals:{...}, flags:[{flag, level, message, known}, ...]}
    """
    async with _client() as ha:
        try:
            result = await build_archbox_pulse(ha)
        except HAError as e:
            return {"error": str(e)}
    return result


@mcp.tool()
async def archbox_health_full() -> dict:
    """FULL archbox sensor suite, sectioned — every channel, not just the vitals.

    The broad read. `archbox_pulse()` is the headline numbers (~550 B); this is
    everything the rig exposes (~2 KB), grouped so you can scan one subsystem at
    a time. Reads InfluxDB directly rather than the 11 HA convenience sensors,
    so multi-sensor arrays come back as arrays.

    Sections:
      cpu           tctl, per-CCD (ccd1/ccd2), load%, load1/5/15, threads,
                    process + running counts
      gpu_die       temp, hotspot, gpu2, hotspot_delta, util%, mem-util%,
                    pstate, graphics/sm/memory clocks, pcie gen + width
      gpu_memory    MEM1-3 (the TRUSTWORTHY GDDR6X readings), vram_junction
                    (flaky per upstream), used/total MiB
      gpu_vrm       pwr1..pwr5 power-stage sensors
      gpu_fans_rpm  fan0/1/2 REAL rpm (nvidia-smi only gives a %, reported
                    separately as gpu_fan_driver_pct)
      loop          coolant_temp, chassis_fans_rpm (fan1..fan6),
                    gpu_over_coolant
      board         NCT6686D thermistors
      memory        dimm_temps[] (all 4 DDR5), used%, used/total GiB, swap%
      storage       nvme_composite_temps[] (all 4 drives) + per-sensor arrays,
                    filesystems_used_pct by mount (incl. /srv/ai-models)
      network       nic_temp, wifi_temp
      power         wall_w (WHOLE system at the plug), gpu_w, non_gpu_w,
                    gpu_limit_w, line_v, today/month kWh, mains_switch
      plus          igpu_temp, uptime_hours, online, summary, flags

    Known-empty by design: chassis fan2 reads 0 (empty header — the 6th T30 is
    on a motherboard header, and the NCT6796D-S is dead under Linux).

    Flags use the same thresholds as archbox_pulse, so the two never disagree,
    plus disk_full:<path> at >=90%.

    Returns:
        {timestamp, online, cpu, gpu_die, gpu_memory, gpu_vrm, gpu_fans_rpm,
         loop, board, memory, storage, network, power, summary, flags, ...}
    """
    if not INFLUX_URL or not INFLUX_HOSTS_TOKEN:
        return {"error": "INFLUX_URL and INFLUX_HOSTS_TOKEN must be set in env/.env."}
    influx = InfluxClient(INFLUX_URL, INFLUX_HOSTS_TOKEN, INFLUX_ORG, timeout=INFLUX_TIMEOUT)
    async with _client() as ha, influx:
        try:
            result = await build_archbox_full(ha, influx)
        except (HAError, InfluxError) as e:
            return {"error": str(e)}
    return _guard_dict("archbox_health_full", result, "narrow the range or use archbox_pulse()")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
