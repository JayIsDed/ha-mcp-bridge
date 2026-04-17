from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .ha_client import HAClient, HAError
from .influx_client import InfluxClient, InfluxError
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

if not HA_TOKEN:
    # Don't crash at import — MCP handshake may still work, but tool calls will fail loud.
    # This lets `ha-mcp-bridge --help` style probes work without a token present.
    import sys

    print(
        "WARN: HA_TOKEN not set. Tool calls will fail until it is populated.",
        file=sys.stderr,
    )


mcp = FastMCP("ha-mcp-bridge")


def _client() -> HAClient:
    return HAClient(HA_URL, HA_TOKEN, timeout=HA_TIMEOUT)


def _influx() -> InfluxClient:
    return InfluxClient(INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, timeout=INFLUX_TIMEOUT)


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

    return [EntityInfo.from_ha(e).model_dump() for e in raw]


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

    return [HistoryPoint.from_ha(p).model_dump() for p in raw]


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

    return dict(results)


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

    return [p.model_dump(exclude_none=True) for p in out]


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

    return {"ok": True, "rows": rows, "n": len(rows)}


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
